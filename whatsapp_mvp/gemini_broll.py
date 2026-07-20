"""WhatsApp MVP 的 b-roll 生成封装：包一层 tools.video.gemini_omni_video。

从一段文字 prompt 生成一小段 b-roll，落到 out_path。
关键：**按真实成片秒数记成本**（Omni 自选 3-10s、无视 duration 提示），
生成后 ffprobe 一下 × $0.10，而不是拿 duration 估算自欺。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_COST_PER_SECOND = 0.10  # $0.10/秒输出（5792 tokens/秒 × $17.5/1M）


def _probe_seconds(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def generate_broll(prompt: str, out_path, aspect: str = "9:16") -> Optional[dict]:
    """生成一段 b-roll。成功返回 {path, seconds, cost_usd, interaction_id}，失败返回 None。

    失败一律返回 None（不抛异常）——调用方（insert_broll）应把它当"这段 b-roll 没生成成"
    优雅跳过，不拖垮整条管线。
    """
    from tools.base_tool import ToolStatus
    from tools.video.gemini_omni_video import GeminiOmniVideo

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tool = GeminiOmniVideo()
    if tool.get_status() != ToolStatus.AVAILABLE:
        logger.warning("gemini_broll: provider 不可用（未配 GEMINI_API_KEY/GOOGLE_API_KEY，或非付费档）")
        return None

    # 用时间码前缀轻微引导往短里压（不保证——模型仍在 3-10s 自选）
    guided = prompt if prompt.strip().startswith("[") else f"[0-4s] {prompt}"

    try:
        result = tool.execute({
            "prompt": guided,
            "operation": "text_to_video",
            "aspect_ratio": aspect,
            "output_path": str(out_path),
        })
    except Exception as e:
        logger.warning(f"gemini_broll: 调用异常: {e}")
        return None

    if not result.success:
        logger.warning(f"gemini_broll: 生成失败: {result.error}")
        return None

    seconds = _probe_seconds(out_path)
    cost = round(_COST_PER_SECOND * seconds, 3) if seconds else result.cost_usd
    logger.info(f"gemini_broll: 生成 {seconds:.1f}s → ${cost} @ {out_path}")
    return {
        "path": str(out_path),
        "seconds": seconds,
        "cost_usd": cost,
        "interaction_id": result.data.get("interaction_id"),
    }