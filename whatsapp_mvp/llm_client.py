# WhatsApp MVP - Shared LLM chat-completion dispatch
#
# Single entry point for "send this system+user prompt to whichever LLM
# provider is configured" (deepseek | openai | claude | custom). Used by
# content_planner.py's plan_content()/plan_filler_removal(), which previously
# only spoke the "custom" OpenAI-compatible-base-URL shape and silently
# no-op'd under LLM_PROVIDER=deepseek/openai/claude even with a valid key —
# they support every provider llm_planner.py does now.

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)

# A single flaky attempt was silently turning into "content_planner ships an
# empty plan" for real jobs even though the same call succeeds moments later
# on retry (confirmed: two consecutive real WhatsApp jobs, same code, one
# came back with zero chapters/data points, the next came back fully
# populated). Retry transient failures a couple of times before giving up.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_BASE_SECONDS = 1.5
# 429/5xx are worth retrying (rate limit, transient server trouble). Other
# 4xx (bad key, bad model, malformed request) will fail identically every
# time, so retrying just adds latency for a guaranteed-to-fail call.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _post_with_retries(
    label: str, url: str, headers: dict, body: dict, timeout: int
) -> Optional[dict]:
    """POST with a small retry budget for transient failures only.

    Returns the parsed JSON response body, or None if every attempt failed
    (already logged) or the failure was non-retryable.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == _MAX_ATTEMPTS:
                logger.error(f"{label} call failed after {attempt} attempts (network): {e}")
                return None
            logger.warning(f"{label} call network error, retrying ({attempt}/{_MAX_ATTEMPTS}): {e}")
            time.sleep(_RETRY_BACKOFF_BASE_SECONDS * attempt)
            continue

        if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS:
            logger.warning(f"{label} call got HTTP {resp.status_code}, retrying ({attempt}/{_MAX_ATTEMPTS})")
            time.sleep(_RETRY_BACKOFF_BASE_SECONDS * attempt)
            continue

        try:
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"{label} call failed (HTTP {resp.status_code}, not retrying): {e}")
            return None
        return resp.json()

    return None


def call_llm_chat(system_prompt: str, user_message: str, *, temperature: float = 0.1) -> Optional[str]:
    """Send a single-turn system+user chat completion to the configured LLM provider.

    Returns the raw text content, or None if no provider is usable or the call failed.
    """
    config = get_config()
    provider = config.llm_provider.lower()

    if provider == "claude":
        api_key = config.llm_api_key
        if not api_key:
            logger.warning("No LLM_API_KEY set for claude provider")
            return None
        data = _post_with_retries(
            "Claude",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body={
                "model": config.llm_model,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=60,
        )
        if data is None:
            return None
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Claude response missing expected shape: {e}")
            return None

    # deepseek / openai / custom all speak the OpenAI-compatible chat/completions shape
    if provider == "deepseek":
        endpoint = "https://api.deepseek.com/chat/completions"
        api_key = config.deepseek_api_key or config.llm_api_key
    elif provider == "openai":
        endpoint = "https://api.openai.com/v1/chat/completions"
        api_key = config.openai_api_key or config.llm_api_key
    elif provider == "custom" or config.llm_base_url:
        base = config.llm_base_url.rstrip("/")
        if not base:
            logger.warning("LLM_PROVIDER=custom but no LLM_BASE_URL set")
            return None
        endpoint = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        api_key = config.llm_api_key
    else:
        logger.warning(f"Unknown LLM provider '{provider}'")
        return None

    if not api_key:
        logger.warning(f"No API key set for provider '{provider}'")
        return None

    data = _post_with_retries(
        provider,
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body={
            "model": config.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    if data is None:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"{provider} response missing expected shape: {e}")
        return None


def call_llm_vision(
    system_prompt: str,
    user_message: str,
    images: list[tuple[str, Path]],
    *, temperature: float = 0.1,
) -> Optional[str]:
    """system+user 提示 + 一张或多张本地图片，发给独立配置的视觉 LLM（默认
    GLM-4.6V-Flash，免费、多模态、允许商用，见 docs.z.ai/guides/vlm/glm-4.6v）。

    跟 call_llm_chat 完全独立配置（VISION_LLM_*，不是 LLM_PROVIDER/LLM_API_KEY）
    ——当前生产的文字 provider 是纯文本的 DeepSeek，没有视觉能力，这是单独的
    一个 provider，只服务于"看图判断"这一个用途。

    images: [(label, path), ...]——label 会紧跟在对应图片前面发给模型。视觉
    模型看不到文件名，唯一能让"一次请求发多张图 + 一份 JSON 输出"按图片编号
    对上号的办法，就是给每张图一段紧邻的文字标签（例如 "Frame 130:"）。

    复用 _post_with_retries 同款的网络重试逻辑（429/5xx/超时重试，其余 4xx
    不重试）。返回原始文本内容；没配 VISION_LLM_API_KEY、读图失败、或调用
    失败时返回 None。
    """
    config = get_config()
    api_key = config.vision_llm_api_key
    if not api_key:
        logger.info("No VISION_LLM_API_KEY configured, skipping vision call")
        return None

    content: list[dict] = []
    for label, path in images:
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError as e:
            logger.warning(f"vision: 读取图片失败 {path}: {e}")
            continue
        mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if not content:
        return None
    content.append({"type": "text", "text": user_message})

    data = _post_with_retries(
        "vision",
        config.vision_llm_base_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body={
            "model": config.vision_llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    if data is None:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"vision response missing expected shape: {e}")
        return None
