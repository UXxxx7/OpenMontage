# WhatsApp MVP - 极简语言检测
#
# 只需要在"中文文案 / 英文文案"两套写死模板之间选一套，不需要识别具体
# 语种——含中日韩表意文字判中文，否则判英文。够用，零依赖，零延迟。

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def detect_lang(text: str) -> str:
    """返回 'zh' 或 'en'。"""
    if text and _CJK_RE.search(text):
        return "zh"
    return "en"
