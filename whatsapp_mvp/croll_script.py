"""C-roll 文案生成：看一张照片，写一段适合数字人口播的短文案。

跟 qa_answer.py 一个模式——按用户语言出对应语言的文案，LLM 不可用时给一句
诚实的兜底而不是让整条 C-roll 流程直接失败在这一步。
"""
from __future__ import annotations

import logging

from .llm_client import call_vision_chat

logger = logging.getLogger(__name__)

_PROMPT_ZH = (
    "这张照片要用来生成一条数字人口播短视频（AI 会让照片里的人物说话）。"
    "请你看图写一段第一人称口播文案，自然口语化、像真人在对着镜头说话，"
    "不要有舞台提示或旁白说明，只输出要说的话本身。"
    "{hint_line}"
    "长度控制在 60-100 字之间（大约 15-25 秒的语速），开头一句要能抓住注意力。"
)
_PROMPT_EN = (
    "This photo will be used to generate a talking-head short video (AI will "
    "animate the person in the photo to speak). Look at the photo and write a "
    "short first-person spoken script — natural and conversational, like someone "
    "talking directly to camera. Output only the spoken words themselves, no "
    "stage directions or narration labels."
    "{hint_line}"
    "Keep it to about 40-70 words (roughly 15-25 seconds spoken), with a strong "
    "attention-grabbing opening line."
)


def write_script(image_path: str, lang: str = "zh", hint: str = "") -> str | None:
    """看图写文案。hint 是用户给的额外提示（比如想推广什么、什么语气），
    可以为空——为空时完全由 AI 自由发挥，看图片本身像该说点什么。
    成功返回文案字符串；LLM 不可用或调用失败返回 None（调用方应把这当
    "这步没成"处理，不硬造一段文案糊弄用户）。
    """
    template = _PROMPT_ZH if lang == "zh" else _PROMPT_EN
    if hint.strip():
        hint_line = (f'用户给的方向提示："{hint.strip()}"，围绕这个来写。'
                    if lang == "zh" else
                    f'The user gave this direction: "{hint.strip()}" — write around it. ')
    else:
        hint_line = ""
    prompt = template.format(hint_line=hint_line)

    result = call_vision_chat(prompt, [image_path])
    if not result or not result.strip():
        logger.warning("croll_script: 视觉 LLM 不可用或未返回内容，C-roll 文案生成失败")
        return None
    return result.strip()
