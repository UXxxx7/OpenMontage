# WhatsApp MVP - Shared LLM chat-completion dispatch
#
# Single entry point for "send this system+user prompt to whichever LLM
# provider is configured" (deepseek | openai | claude | custom). Used by
# content_planner.py's plan_content()/plan_filler_removal(), which previously
# only spoke the "custom" OpenAI-compatible-base-URL shape and silently
# no-op'd under LLM_PROVIDER=deepseek/openai/claude even with a valid key —
# they support every provider llm_planner.py does now.

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)

# requests' own `timeout=` only bounds a single socket read, not the total
# call duration — if the server dribbles bytes slowly enough that no single
# read ever stalls past `timeout`, the countdown keeps getting reset and the
# call can run far longer than the configured timeout ever implies. Confirmed
# real cases (2026-07-27 investigation, DeepSeek): a call configured with
# timeout=60 took ~17 minutes before finally erroring; 65 network errors
# total in this deployment's log history, 19 in one evening alone. Enforce an
# actual wall-clock cap by running the request in a worker thread and giving
# up on waiting for it once `_HARD_CALL_DEADLINE_S` elapses — this does NOT
# cancel the underlying request (Python threads can't be killed; the socket
# call keeps running until the OS/remote eventually gives up on its own), it
# just stops the caller from blocking on it, so a slow-trickling server can
# no longer stall the whole pipeline for minutes past what `timeout=` implies.
#
# 2026-07-28 架构复审（延迟优化）：原来设的 90s 偏保守——call_llm_chat 自己的
# docstring 早就记录过 DeepSeek 网关对非流式响应有 ~60s 硬时限（v4-flash
# 实测 37s 完成，v4-pro 写不完长 JSON，60s 整被网关掐断）——也就是说任何
# 真正会成功的单次调用，DeepSeek 自己最迟也就在 60s 左右给出结果或直接掐断，
# 不存在"合法但需要 90s"的调用。留 15s 余量设到 75s：比原来的 90s 更快发现
# 真正卡住的连接，同时不会比 DeepSeek 自己的硬时限更早误伤正常调用。
_HARD_CALL_DEADLINE_S = 75
_call_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-http")

# 同一进程内所有调用复用一个 Session——默认的 requests.post(...) 每次都会
# 新开一条 TCP+TLS 连接，同一个 job 里对同一个 host（api.deepseek.com）的
# 多次调用完全可以复用连接，省掉重复握手的延迟。纯性能优化，不改变任何
# 重试/超时语义（Session 对象本身线程安全，可以跨 _call_executor 的多个
# 工作线程共享）。
_session = requests.Session()


def _post_bounded(
    url: str, headers: dict, body: dict, timeout: int, hard_deadline_s: Optional[float] = None
) -> requests.Response:
    """hard_deadline_s：不传就用 `_HARD_CALL_DEADLINE_S`（按 DeepSeek 非流式
    调用的实际时长调的，75s）——但不是每个调用方都是同一种工作量。scene_
    author.py 现写/修订场景代码是明显更重的调用（大 token 输出、"思考型"
    模型），硬套 75s 会把本该成功、只是本来就需要更久的调用提前误杀，这不
    是这个硬上限机制原本要解决的问题（要解决的是"服务器挤牙膏导致的单次
    调用无限期卡住"，不是"限制所有调用必须多快完成"）。调用方按自己实际
    的合理调用时长传一个更大的值。"""
    future = _call_executor.submit(_session.post, url, headers=headers, json=body, timeout=timeout)
    return future.result(timeout=hard_deadline_s if hard_deadline_s is not None else _HARD_CALL_DEADLINE_S)

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
            resp = _post_bounded(url, headers, body, timeout)
        # ChunkedEncodingError（"Response ended prematurely"）不是 ConnectionError/
        # Timeout 的子类——响应体传输中途被切断时抛的是这个，之前漏抓，导致一次
        # 廉价的传输层抖动被迫升级成调用方（apply_style）整段重跑。
        # concurrent.futures.TimeoutError：_post_bounded 的硬性总耗时上限触发——
        # 服务器间歇性挤字节导致 requests 自己的 timeout 一直没触发时兜底。
        except (
            requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
            concurrent.futures.TimeoutError,
        ) as e:
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


def call_llm_chat(system_prompt: str, user_message: str, *, temperature: float = 0.1,
                  model: Optional[str] = None, json_mode: bool = True) -> Optional[str]:
    """Send a single-turn system+user chat completion to the configured LLM provider.

    Returns the raw text content, or None if no provider is usable or the call failed.

    model: 覆盖 config.llm_model。长 JSON 输出的调用（内容规划等）应传
    config.llm_model_long_output —— DeepSeek 网关对非流式响应有 ~60s 硬时限，
    v4-pro 写不完长 JSON（实测 60s 整被掐），v4-flash 37s 完成。

    json_mode: 默认 True（沿用所有既有调用方的行为——内容规划/口误检测都要
    JSON）。自由文本问答这类要的是给用户看的自然语言，不是 JSON，传 False。
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
                "model": model or config.llm_model,
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

    body = {
        "model": model or config.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = _post_with_retries(
        provider,
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body=body,
        timeout=60,
    )
    if data is None:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"{provider} response missing expected shape: {e}")
        return None


def call_vision_chat(text_prompt: str, image_paths: list, timeout: int = 90):
    """视觉子能力调用（独立于主 LLM 通道）。

    主规划走 LLM_*（DeepSeek，纯文本模型）；这里走 VISION_LLM_*（如智谱
    GLM-4V）——只在需要"看图"的环节使用（QA stills 复审等）。未配置
    VISION_LLM_API_KEY 时返回 None，调用方按"没有眼睛"跳过，不影响主流程。

    图片以 base64 data URL 内联（OpenAI 兼容 content-parts 格式，智谱/
    Gemini/OpenAI 通用），不依赖公网可访问的图床。
    """
    import base64
    from pathlib import Path

    from .config import get_config

    config = get_config()
    if not config.vision_llm_api_key or not config.vision_llm_base_url:
        logger.info("视觉 LLM 未配置（VISION_LLM_*），跳过看图环节")
        return None

    content: list = []
    for p in image_paths:
        p = Path(p)
        if not p.exists():
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    if not content:
        return None
    content.append({"type": "text", "text": text_prompt})

    endpoint = config.vision_llm_base_url.rstrip("/") + "/chat/completions"
    for attempt in range(2):
        try:
            resp = _post_bounded(
                endpoint,
                {"Authorization": f"Bearer {config.vision_llm_api_key}",
                 "Content-Type": "application/json"},
                {"model": config.vision_llm_model,
                 "messages": [{"role": "user", "content": content}],
                 "temperature": 0.2},
                timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except (requests.exceptions.RequestException, concurrent.futures.TimeoutError) as e:
            if attempt == 0:
                import time as _time
                logger.warning(f"视觉 LLM 连接层错误，5s 后重试: {e}")
                _time.sleep(5)
                continue
            logger.error(f"视觉 LLM 调用失败: {e}")
            return None
        except Exception as e:
            logger.error(f"视觉 LLM 调用失败: {e}")
            return None
    return None
