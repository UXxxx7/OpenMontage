"""背景音乐 provider 选择层（占位，可扩展）。仿 broll_providers.py 的模式。

当前只接了 Pixabay（免费曲库检索，零 key）。未来加 Suno / ElevenLabs 这类
AI 现场作曲：在下面加一个分支 + 往 AVAILABLE_MUSIC_PROVIDERS 加一项即可，
_op_add_music 不用再改（tools/audio/ 下 suno_music.py / music_gen.py 已经
是现成的 provider，接线时挂进来）。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

AVAILABLE_MUSIC_PROVIDERS = ["pixabay"]
DEFAULT_MUSIC_PROVIDER = "pixabay"


def fetch_music_via(provider: Optional[str], query: str, out_path, min_duration: float = 30,
                    max_duration: float = 180) -> Optional[dict]:
    """按所选 provider 找一段背景音乐。

    成功返回 {path, seconds, cost_usd}；失败或未知 provider 返回 None
    （供上层——add_music——把它当"这段没配成"优雅跳过，不拖垮管线）。
    """
    provider = (provider or DEFAULT_MUSIC_PROVIDER).lower()

    if provider == "pixabay":
        from .pixabay_bg_music import fetch_music
        return fetch_music(query, out_path, min_duration, max_duration)

    # --- 占位：未来 provider（AI 现场作曲，真花钱）---
    # if provider == "suno":
    #     from .suno_bg_music import generate_music_suno
    #     return generate_music_suno(query, out_path)

    logger.warning(
        f"fetch_music_via: 未接线的 provider '{provider}'（当前仅支持 {AVAILABLE_MUSIC_PROVIDERS}）"
    )
    return None
