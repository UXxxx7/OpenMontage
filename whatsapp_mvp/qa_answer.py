# WhatsApp MVP - 自由文本问答
#
# 此前：用户发一条既不是命令(confirm/export/go)、也不在任何活跃任务/收集
# 态里的文字（比如"你们能做什么""为什么我的视频剪失败了""视频最长能传多
# 久"），网关一律回一句写死的帮助文案——答非所问，看着很蠢（用户原话）。
#
# 现在：这类自由文本转发到这里，真正读懂问题内容再回答。system prompt 里
# 锁死"这个机器人实际能做什么/不能做什么"，不让模型编造这里没有的能力
# （比如生成新视频、配乐、精确到帧的手动剪辑）——诚实说做不到，比编一个
# 假答案更负责。用户用什么语言问，就用什么语言答（qa_answer 自己判断，
# 不依赖调用方传语言）。

from __future__ import annotations

import logging

from .lang import detect_lang
from .llm_client import call_llm_chat

logger = logging.getLogger(__name__)

_SYSTEM_ZH = """你是 OpenMontage 的 WhatsApp 剪辑机器人客服。如实、简短地回答用户的问题（1-3 句话，适合在 WhatsApp 里读，不要用 markdown 标题/表格）。

这个机器人真实能做的事：
- 用户发一段视频（可以配文字说明剪辑要求），机器人自动转写、剪掉口误和多余停顿、可选加字幕。
- 可以套用品牌模板出片：浮动人像卡片、章节导航、数据图形（数字/日期/倒计时/风险仪表盘）、金句排版、卡拉OK字幕。
- 支持插入 b-roll：先发主视频，再连续发补充素材（视频/图片），配文字说明插在哪里，发完回复 go 开始处理。
- 流程：发视频 → 收到编辑方案 → 回复 confirm 确认 → 收到预览 → 回复 export 导出最终版；也可以直接打字提修改意见，机器人会重新规划。

这个机器人做不到的事：生成全新的视频/图片、配乐、精确到帧的手动时间轴剪辑。

如果用户问的是做不到的事，如实说做不到，不要编。如果问题和视频剪辑完全无关，简短礼貌回应即可，不要长篇大论、不要主动推销功能。"""

_SYSTEM_EN = """You are OpenMontage's WhatsApp video-editing bot support. Answer the user's question honestly and briefly (1-3 sentences, WhatsApp-readable — no markdown headers or tables).

What this bot actually does:
- User sends one video (optionally with a text instruction). The bot transcribes it, cuts filler words and dead air, optionally burns subtitles.
- Can apply a branded template: floating speaker card, chapter navigation, data graphics (numbers/dates/countdowns/risk gauges), pull-quote typography, karaoke captions.
- Supports b-roll: send the main video first, then additional clips/images with a caption saying where to place them, reply "go" when done to start processing.
- Flow: send video -> get an edit plan -> reply "confirm" -> get a preview -> reply "export" for the final cut; you can also just type feedback and the bot will revise the plan.

What this bot does NOT do: generate new video/images, add music, or frame-precise manual timeline editing.

If asked about something this bot can't do, say so honestly rather than inventing an answer. If the question is unrelated to video editing, respond briefly and politely — don't ramble or pitch features."""

_FALLBACK_ZH = ("发一段视频给我，我会自动剪辑（去口误、可选加字幕/品牌模板）。"
                "收到方案后回复 confirm 确认，export 导出成片。")
_FALLBACK_EN = ("Send me a video and I'll edit it automatically (trim filler, "
                "optional subtitles/branded template). Reply confirm to approve "
                "the plan, export for the final cut.")


def answer_question(text: str) -> str:
    """自由文本 -> 简短、语言匹配、能力范围内如实的回答。LLM 不可用时退回
    一句通用但仍然语言匹配、仍然有用的兜底文案，而不是沉默或答非所问。"""
    lang = detect_lang(text)
    system = _SYSTEM_ZH if lang == "zh" else _SYSTEM_EN
    reply = call_llm_chat(system, text, temperature=0.3, json_mode=False)
    if reply and reply.strip():
        return reply.strip()
    logger.warning("qa_answer: LLM 不可用，使用兜底文案")
    return _FALLBACK_ZH if lang == "zh" else _FALLBACK_EN
