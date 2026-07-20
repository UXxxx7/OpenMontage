"""b-roll 生成 provider 选择层（占位，可扩展）。

当前只接了 Gemini Omni。未来加 Veo / Seedance 等：在下面加一个分支 +
往 AVAILABLE_BROLL_PROVIDERS 加一项即可，_op_insert_broll 不用再改。
（你们 tools/ 里已有 veo_video / seedance_video 等 provider，接线时挂进来。）
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 供上层（指令/规划层）展示"可选项"。多接一个 provider 就多一项。
AVAILABLE_BROLL_PROVIDERS = ["omni"]
DEFAULT_BROLL_PROVIDER = "omni"


def generate_broll_via(provider: Optional[str], prompt: str, out_path, aspect: str = "9:16") -> Optional[dict]:
    """按所选 provider 生成一段 b-roll。

    成功返回 {path, seconds, cost_usd, interaction_id}；失败或未知 provider 返回 None
    （供上层——insert_broll——把它当"这段没生成成"优雅跳过，不拖垮管线）。
    """
    provider = (provider or DEFAULT_BROLL_PROVIDER).lower()

    if provider == "omni":
        from .gemini_broll import generate_broll
        return generate_broll(prompt, out_path, aspect)

    # --- 占位：未来 provider ---
    # if provider == "veo":
    #     from .broll_veo import generate_broll_veo
    #     return generate_broll_veo(prompt, out_path, aspect)
    # if provider == "seedance":
    #     ...

    logger.warning(
        f"generate_broll_via: 未接线的 provider '{provider}'（当前仅支持 {AVAILABLE_BROLL_PROVIDERS}）"
    )
    return None