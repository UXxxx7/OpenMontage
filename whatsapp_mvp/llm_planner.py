# WhatsApp MVP - LLM Intent Planner
# Converts natural language edit requests into structured job JSON.

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON Schema for structured edit plan
# ---------------------------------------------------------------------------

EDIT_PLAN_SCHEMA = {
    "type": "object",
    "required": ["pipeline", "edit_operations", "summary"],
    "properties": {
        "pipeline": {
            "type": "string",
            "enum": ["talking-head"],
            "description": "The pipeline to use",
        },
        "edit_operations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "description"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "trim_leading_silence",
                            "trim_trailing_silence",
                            "remove_repetition",
                            "remove_interruption",
                            "add_subtitles",
                            "smooth_cuts",
                            "speed_up_silence",
                            "keep_segment",
                            "remove_segment",
                        ],
                    },
                    "description": {"type": "string"},
                    "language": {"type": "string"},
                },
            },
        },
        "summary": {
            "type": "string",
            "description": "Human-readable summary of planned edits",
        },
        "clarification_needed": {
            "type": "boolean",
            "description": "True if the LLM is unsure and needs user clarification",
        },
        "clarification_question": {
            "type": "string",
            "description": "Question for the user if clarification is needed",
        },
    },
}

SYSTEM_PROMPT = """You are a video editing assistant. Given a user's natural language edit request for a talking-head video, output a structured JSON edit plan.

Available edit operations:
- trim_leading_silence: Remove blank/silent beginning of the video
- trim_trailing_silence: Remove blank/silent ending
- remove_repetition: Remove repeated/duplicate speech segments
- remove_interruption: Remove self-interrupted or stuttering parts
- add_subtitles: Add subtitles (specify language, e.g. "zh" for Chinese, "en" for English)
- smooth_cuts: Make transitions between cuts smooth and natural
- speed_up_silence: Speed up silent pauses instead of cutting them
- keep_segment: Explicitly keep a specific section
- remove_segment: Explicitly remove a specific section

Rules:
1. If the user's request is ambiguous, set clarification_needed to true and ask exactly one clear question.
2. Always output valid JSON matching the schema.
3. For Chinese subtitle requests, set language to "zh".
4. Keep the summary concise and friendly, in the same language as the user's request.
5. For talking-head videos, default assumptions:
   - Remove leading blank segments (>1 second of silence at start)
   - Keep the speaker centered and visible
   - Cut out obvious stutters and false starts
   - Maintain natural pacing (don't over-cut)"""


# ---------------------------------------------------------------------------
# LLM Planner
# ---------------------------------------------------------------------------

class LLMPlanner:
    """Converts natural language to structured edit plans using LLM."""

    def __init__(self):
        self.config = get_config()

    def plan(self, edit_request: str, video_duration: Optional[float] = None) -> dict[str, Any]:
        """Generate a structured edit plan from a natural language request.

        Args:
            edit_request: The user's edit request in natural language.
            video_duration: Optional video duration in seconds for context.

        Returns:
            Dict with the structured edit plan.
        """
        provider = self.config.llm_provider.lower()

        # custom / 中转站：OpenAI 兼容的自定义端点（优先）
        if provider == "custom" or self.config.llm_base_url:
            return self._call_custom(edit_request, video_duration)
        elif provider == "deepseek":
            return self._call_deepseek(edit_request, video_duration)
        elif provider == "openai":
            return self._call_openai(edit_request, video_duration)
        elif provider == "claude":
            return self._call_claude(edit_request, video_duration)
        else:
            logger.warning(f"Unknown LLM provider '{provider}', using default planner")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # DeepSeek
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Custom / 中转站（OpenAI 兼容端点）
    # ------------------------------------------------------------------

    def _call_custom(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        """通过中转站（OpenAI 兼容 API）调用 LLM。

        设置 LLM_PROVIDER=custom 并在 .env 中配置:
          LLM_BASE_URL=https://your-proxy.com/v1
          LLM_API_KEY=sk-your-key
          LLM_MODEL=gpt-4o-mini
        """
        api_key = self.config.llm_api_key
        base_url = self.config.llm_base_url

        if not api_key:
            logger.warning("No LLM_API_KEY set; using default planner")
            return self._default_plan(edit_request)

        # 确保 base_url 以 /v1 结尾（OpenAI 兼容格式）
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        endpoint += "/chat/completions"

        user_message = self._build_user_message(edit_request, video_duration)

        try:
            resp = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.llm_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return self._parse_and_validate(content)
        except Exception as e:
            logger.error(f"Custom LLM call failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response body: {e.response.text[:500]}")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # DeepSeek
    # ------------------------------------------------------------------

    def _call_deepseek(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.deepseek_api_key
        if not api_key:
            logger.warning("No DEEPSEEK_API_KEY set; using default planner")
            return self._default_plan(edit_request)

        user_message = self._build_user_message(edit_request, video_duration)

        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return self._parse_and_validate(content)

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    def _call_openai(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.openai_api_key
        if not api_key:
            logger.warning("No OPENAI_API_KEY set; using default planner")
            return self._default_plan(edit_request)

        user_message = self._build_user_message(edit_request, video_duration)

        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return self._parse_and_validate(content)

    # ------------------------------------------------------------------
    # Claude (Anthropic)
    # ------------------------------------------------------------------

    def _call_claude(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.openai_api_key  # Reuse or add ANTHROPIC_API_KEY
        if not api_key:
            logger.warning("No API key set; using default planner")
            return self._default_plan(edit_request)

        user_message = self._build_user_message(edit_request, video_duration)

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.llm_model,
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["content"][0]["text"]
        return self._parse_and_validate(content)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_user_message(self, edit_request: str, video_duration: Optional[float] = None) -> str:
        parts = [f"User's edit request: {edit_request}"]
        if video_duration:
            parts.append(f"Video duration: {video_duration:.0f} seconds")
        parts.append("Output a JSON edit plan.")
        return "\n".join(parts)

    def _parse_and_validate(self, raw_content: str) -> dict:
        """Parse LLM output and validate against schema."""
        try:
            plan = json.loads(raw_content)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            import re
            match = re.search(r"`(?:json)?\s*([\s\S]*?)`", raw_content)
            if match:
                plan = json.loads(match.group(1))
            else:
                logger.error(f"Failed to parse LLM output: {raw_content[:200]}")
                return self._default_plan("fallback")

        # Validate required fields
        if "pipeline" not in plan:
            plan["pipeline"] = "talking-head"
        if "edit_operations" not in plan:
            plan["edit_operations"] = []
        if "summary" not in plan:
            plan["summary"] = "Edit plan generated automatically."

        return plan

    def _default_plan(self, edit_request: str) -> dict:
        """Fallback when no LLM is available."""
        request_lower = edit_request.lower()

        operations = []

        # Detect common patterns
        if any(w in request_lower for w in ("blank", "silence", "silent", "empty", "leading", "beginning")):
            operations.append({
                "type": "trim_leading_silence",
                "description": "Remove blank/silent beginning",
            })

        if any(w in request_lower for w in ("repeat", "repeated", "duplicate", "repetition")):
            operations.append({
                "type": "remove_repetition",
                "description": "Remove repeated or duplicate speech segments",
            })

        if any(w in request_lower for w in ("interrupt", "stutter", "self-interrupt", "self interrupt")):
            operations.append({
                "type": "remove_interruption",
                "description": "Remove self-interrupted or stuttering parts",
            })

        if any(w in request_lower for w in ("subtitle", "caption", "subtitles", "captions")):
            language = "zh" if any(w in request_lower for w in ("chinese", "中文", "cn")) else "en"
            operations.append({
                "type": "add_subtitles",
                "language": language,
                "description": f"Add {'Chinese' if language == 'zh' else 'English'} subtitles",
            })

        if any(w in request_lower for w in ("smooth", "flow", "natural", "seamless")):
            operations.append({
                "type": "smooth_cuts",
                "description": "Make transitions between cuts smooth and natural",
            })

        if not operations:
            operations = [
                {"type": "trim_leading_silence", "description": "Remove blank beginning"},
                {"type": "remove_repetition", "description": "Remove repeated parts"},
                {"type": "add_subtitles", "language": "zh", "description": "Add Chinese subtitles"},
            ]

        return {
            "pipeline": "talking-head",
            "edit_operations": operations,
            "summary": f"I'll edit your talking-head video: {', '.join(op['description'] for op in operations)}.",
            "clarification_needed": False,
            "clarification_question": None,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_planner: Optional[LLMPlanner] = None


def get_planner() -> LLMPlanner:
    global _planner
    if _planner is None:
        _planner = LLMPlanner()
    return _planner


def plan_edit(edit_request: str, video_duration: Optional[float] = None) -> dict:
    """Convenience function: plan an edit from natural language."""
    return get_planner().plan(edit_request, video_duration)

