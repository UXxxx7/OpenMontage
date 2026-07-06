# WhatsApp MVP - LLM Intent Planner
# 把用户的自然语言编辑请求转成结构化编辑计划（L1.5：能力清单 + 标记 unsupported）。

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 能力清单（capability envelope）—— 只有这些操作能真正执行（对应正式工具）
# ---------------------------------------------------------------------------

SUPPORTED_OPERATIONS = [
    "trim_start",            # 剪掉开头 N 秒        {"seconds": N}
    "trim_end",              # 剪掉结尾 N 秒        {"seconds": N}
    "keep_range",            # 只保留某段          {"start_seconds": S, "end_seconds": E}
    "remove_silences",       # 去掉所有静音/停顿    （无参）
    "trim_leading_silence",  # 只去掉开头的静音     （无参）
    "speed_up_silence",      # 静音段加速而非删除   {"factor": F} 可选
    "add_subtitles",         # 转写并烧录字幕(原语言){"language": "..."} 可选
]

EDIT_PLAN_SCHEMA = {
    "type": "object",
    "required": ["edit_operations", "summary"],
    "properties": {
        "edit_operations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": SUPPORTED_OPERATIONS},
                    "seconds": {"type": "number"},
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "language": {"type": "string"},
                    "factor": {"type": "number"},
                    "description": {"type": "string"},
                },
            },
        },
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "clarification_needed": {"type": "boolean"},
        "clarification_question": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You are a video-editing planner for talking-head videos. Convert the user's natural-language request (it may be in Chinese or English) into a structured JSON edit plan.

You can ONLY use these operations. Do NOT invent any others:
- trim_start: remove the first N seconds. params: {"seconds": N}
- trim_end: remove the last N seconds. params: {"seconds": N}
- keep_range: keep only one time range, drop the rest. params: {"start_seconds": S, "end_seconds": E}
- remove_silences: remove ALL silent pauses / dead air to make the video compact. no params.
- trim_leading_silence: remove ONLY the silent gap at the very beginning. no params.
- speed_up_silence: speed up silent pauses instead of cutting them. params: {"factor": F} (optional).
- add_subtitles: transcribe the spoken audio and burn captions onto the video. params: {"language": "..."} (optional).

Mapping guidance (examples):
- "减掉/剪掉/去掉开头10秒" / "cut the first 10 seconds" -> trim_start {"seconds": 10}
- "去掉结尾5秒" / "remove last 5 seconds" -> trim_end {"seconds": 5}
- "只保留第30到60秒" -> keep_range {"start_seconds": 30, "end_seconds": 60}
- "去掉停顿/删掉空白/让它更紧凑连贯/流畅一点" -> remove_silences
- "去掉开头的空白/开头静音" -> trim_leading_silence
- "把停顿加速" -> speed_up_silence
- "加字幕/配字幕" -> add_subtitles

Rules:
1. Map the user's intent to the operations above. Every operation object must have a "type" and a short "description" written in the SAME language as the user.
2. If the user asks for something NOT in the list (e.g. remove repeated sentences / stutters / filler words, TRANSLATE subtitles to another language, add background music, change/replace the background, reframe/crop to vertical, color grading, add titles/logos/overlays), DO NOT fake it. Put a short human-readable description of each unsupported request into the "unsupported" array, and only put the operations you CAN do into "edit_operations".
3. About subtitles: add_subtitles transcribes the SPOKEN language only — it cannot translate. If the user asks for subtitles in a language that is likely different from the speech (e.g. "加中文字幕" on an English-spoken video), still include add_subtitles, but ALSO add a note to "unsupported" such as "翻译字幕到中文（当前只支持原语言字幕）".
4. "summary" is one short friendly sentence in the user's language describing what you will do (and briefly what you cannot, if anything).
5. If the request is too vague to map to any operation, set "clarification_needed": true and ask exactly ONE question in "clarification_question".
6. Always output valid JSON only, matching the schema. No markdown, no prose outside the JSON."""


# ---------------------------------------------------------------------------
# LLM Planner
# ---------------------------------------------------------------------------

class LLMPlanner:
    """把自然语言转成结构化编辑计划。"""

    def __init__(self):
        self.config = get_config()

    def plan(self, edit_request: str, video_duration: Optional[float] = None) -> dict[str, Any]:
        provider = self.config.llm_provider.lower()

        # custom / 中转站（OpenAI 兼容端点，如 DeepSeek 中转）优先
        if provider == "custom" or self.config.llm_base_url:
            return self._call_custom(edit_request, video_duration)
        elif provider == "deepseek":
            return self._call_deepseek(edit_request, video_duration)
        elif provider == "openai":
            return self._call_openai(edit_request, video_duration)
        elif provider == "claude":
            return self._call_claude(edit_request, video_duration)
        else:
            logger.warning(f"Unknown LLM provider '{provider}', using keyword planner")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # Custom / 中转站（OpenAI 兼容端点）
    # ------------------------------------------------------------------

    def _call_custom(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        """通过中转站（OpenAI 兼容 API，如 DeepSeek）调用 LLM。

        .env 配置：
          LLM_PROVIDER=custom
          LLM_BASE_URL=https://你的中转站域名   （自动补 /v1/chat/completions）
          LLM_API_KEY=sk-...
          LLM_MODEL=deepseek-chat
        """
        api_key = self.config.llm_api_key
        base_url = self.config.llm_base_url

        if not api_key:
            logger.warning("No LLM_API_KEY set; using keyword planner")
            return self._default_plan(edit_request)

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
            if hasattr(e, "response") and getattr(e, "response", None) is not None:
                logger.error(f"Response body: {e.response.text[:500]}")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # DeepSeek 官方直连
    # ------------------------------------------------------------------

    def _call_deepseek(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.deepseek_api_key
        if not api_key:
            logger.warning("No DEEPSEEK_API_KEY set; using keyword planner")
            return self._default_plan(edit_request)
        return self._call_openai_compatible(
            "https://api.deepseek.com/chat/completions", api_key, edit_request, video_duration
        )

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    def _call_openai(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.openai_api_key
        if not api_key:
            logger.warning("No OPENAI_API_KEY set; using keyword planner")
            return self._default_plan(edit_request)
        return self._call_openai_compatible(
            "https://api.openai.com/v1/chat/completions", api_key, edit_request, video_duration
        )

    def _call_openai_compatible(
        self, url: str, api_key: str, edit_request: str, video_duration: Optional[float] = None
    ) -> dict:
        user_message = self._build_user_message(edit_request, video_duration)
        try:
            resp = requests.post(
                url,
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
            logger.error(f"LLM call failed: {e}")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # Claude (Anthropic)
    # ------------------------------------------------------------------

    def _call_claude(self, edit_request: str, video_duration: Optional[float] = None) -> dict:
        api_key = self.config.openai_api_key  # 或另配 ANTHROPIC_API_KEY
        if not api_key:
            logger.warning("No API key set; using keyword planner")
            return self._default_plan(edit_request)

        user_message = self._build_user_message(edit_request, video_duration)
        try:
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
        except Exception as e:
            logger.error(f"Claude call failed: {e}")
            return self._default_plan(edit_request)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_user_message(self, edit_request: str, video_duration: Optional[float] = None) -> str:
        parts = [f"User's edit request: {edit_request}"]
        if video_duration:
            parts.append(f"Video duration: {video_duration:.0f} seconds")
        parts.append("Output the JSON edit plan.")
        return "\n".join(parts)

    def _parse_and_validate(self, raw_content: str) -> dict:
        """解析 LLM 输出并规整。过滤掉不支持的操作类型，防止执行器拿到未知 op。"""
        plan = None
        try:
            plan = json.loads(raw_content)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", raw_content)
            if match:
                try:
                    plan = json.loads(match.group(0))
                except json.JSONDecodeError:
                    plan = None
        if not isinstance(plan, dict):
            logger.error(f"Failed to parse LLM output: {raw_content[:200]}")
            return self._default_plan("fallback")

        plan.setdefault("edit_operations", [])
        plan.setdefault("summary", "编辑计划已生成")
        plan.setdefault("unsupported", [])

        # 规整每个操作：展平嵌套 params，只保留受支持的操作
        clean_ops = []
        for op in plan.get("edit_operations", []):
            if not isinstance(op, dict):
                continue
            # 有些模型会把参数放进 op["params"]，展平到 op 顶层供执行器读取
            params = op.pop("params", None)
            if isinstance(params, dict):
                for k, v in params.items():
                    op.setdefault(k, v)
            if op.get("type") in SUPPORTED_OPERATIONS:
                clean_ops.append(op)
            elif op.get("type"):
                plan["unsupported"].append(f"未支持的操作: {op.get('type')}")
        plan["edit_operations"] = clean_ops
        return plan

    # ------------------------------------------------------------------
    # 关键词兜底（无 LLM key 时）—— 中英文
    # ------------------------------------------------------------------

    def _default_plan(self, edit_request: str) -> dict:
        req = (edit_request or "").lower()
        ops: list[dict] = []
        unsupported: list[str] = []

        # 剪掉开头 N 秒（数字）
        m = re.search(r"(?:开头|前面?)[^\d]{0,4}(\d+)\s*秒", req) or re.search(r"first\s*(\d+)\s*sec", req)
        if m:
            n = float(m.group(1))
            ops.append({"type": "trim_start", "seconds": n, "description": f"剪掉开头 {int(n)} 秒"})
        elif any(w in req for w in ("开头空白", "开头静音", "去掉开头", "trim beginning", "leading silence")):
            ops.append({"type": "trim_leading_silence", "description": "去掉开头的静音/空白"})

        # 剪掉结尾 N 秒
        me = re.search(r"(?:结尾|末尾|最后)[^\d]{0,4}(\d+)\s*秒", req) or re.search(r"last\s*(\d+)\s*sec", req)
        if me:
            n = float(me.group(1))
            ops.append({"type": "trim_end", "seconds": n, "description": f"剪掉结尾 {int(n)} 秒"})

        # 去静音
        if any(w in req for w in ("停顿", "静音", "空白", "紧凑", "连贯", "流畅",
                                   "silence", "silent", "pause", "blank", "smooth", "compact")):
            if not any(o["type"] == "trim_leading_silence" for o in ops):
                ops.append({"type": "remove_silences", "description": "去掉停顿使视频更紧凑"})

        # 字幕
        if any(w in req for w in ("字幕", "subtitle", "caption")):
            want_zh = any(w in req for w in ("中文", "chinese"))
            op = {"type": "add_subtitles", "description": "添加字幕（原语言）"}
            if want_zh:
                op["language"] = "zh"
                unsupported.append("翻译字幕到中文（当前只支持原语言字幕）")
            ops.append(op)

        # 明确标记做不了的常见需求
        if any(w in req for w in ("重复", "结巴", "口误", "repeat", "repetition", "stutter", "filler")):
            unsupported.append("删除重复/结巴/口误片段（需要语义分析，暂不支持）")
        if any(w in req for w in ("配乐", "音乐", "背景音乐", "music", "bgm")):
            unsupported.append("添加背景音乐（暂不支持）")
        if any(w in req for w in ("竖屏", "横屏", "reframe", "裁剪画面", "crop", "9:16")):
            unsupported.append("画面重构图/竖屏（暂不支持）")

        if not ops:
            ops = [{"type": "remove_silences", "description": "去掉停顿使视频更紧凑"}]

        summary = "我会：" + "、".join(o["description"] for o in ops) + "。"
        if unsupported:
            summary += "（暂时做不了：" + "、".join(unsupported) + "）"

        return {
            "edit_operations": ops,
            "unsupported": unsupported,
            "summary": summary,
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
    return get_planner().plan(edit_request, video_duration)
