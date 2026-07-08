# WhatsApp MVP - Shared LLM chat-completion dispatch
#
# Single entry point for "send this system+user prompt to whichever LLM
# provider is configured" (deepseek | openai | claude | custom). Used by
# content_planner.py's plan_content()/plan_filler_removal(), which previously
# only spoke the "custom" OpenAI-compatible-base-URL shape and silently
# no-op'd under LLM_PROVIDER=deepseek/openai/claude even with a valid key —
# they support every provider llm_planner.py does now.

from __future__ import annotations

import logging
from typing import Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)


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
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.llm_model,
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as e:
            logger.error(f"Claude call failed: {e}")
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

    # 免费档 LLM（如 Gemini free tier：5-15 RPM）很容易被 L2 循环 + 内容规划
    # 的连续调用打到 429。429 是"等一下再来"不是"坏了"——退避重试两次，
    # 而不是直接放弃导致整条内容规划降级为空。
    for attempt in range(3):
        try:
            resp = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
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
            if resp.status_code == 429 and attempt < 2:
                import time as _time

                wait = 20 * (attempt + 1)
                logger.warning(f"LLM 429 (rate limit)，{wait}s 后重试（第 {attempt + 1} 次）")
                _time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            # 连接层故障（Response ended prematurely / 超时 / 断连）是暂时的，
            # 重试一次往往就过——直接放弃会让整条内容规划降级为空。
            if attempt < 2:
                import time as _time

                logger.warning(f"LLM 连接层错误，5s 后重试（第 {attempt + 1} 次）: {e}")
                _time.sleep(5)
                continue
            logger.error(f"LLM call failed ({provider}): {e}")
            return None
        except Exception as e:
            logger.error(f"LLM call failed ({provider}): {e}")
            return None
    return None
