"""WhatsApp MVP 的背景音乐检索封装：包一层 tools.audio.pixabay_music。

免费曲库检索，零 API key、零花费（Pixabay Content License，免署名可商用）。
工具本身标注 EXPERIMENTAL（爬公开搜索页，非官方 API，页面改版可能失效）——
所以这里失败一律返回 None，不抛异常，调用方（add_music）应把它当"这段
背景音乐没配成"优雅跳过，不拖垮整条管线。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_music(query: str, out_path, min_duration: float = 30,
                max_duration: float = 180) -> Optional[dict]:
    """检索并下载一段背景音乐。成功返回 {path, seconds, cost_usd=0.0}，失败返回 None。"""
    from tools.base_tool import ToolStatus
    from tools.audio.pixabay_music import PixabayMusic

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tool = PixabayMusic()
    if tool.get_status() != ToolStatus.AVAILABLE:
        logger.warning("pixabay_bg_music: provider 不可用")
        return None

    try:
        result = tool.execute({
            "query": query,
            "min_duration": min_duration,
            "max_duration": max_duration,
        })
    except Exception as e:
        logger.warning(f"pixabay_bg_music: 调用异常: {e}")
        return None

    if not result.success:
        logger.warning(f"pixabay_bg_music: 检索失败: {result.error}")
        return None

    # Pixabay 工具把下载好的文件路径放在 artifacts[0]，不一定等于我们传的 out_path
    # （工具内部按曲目名字自己起的文件名）——统一挪到 out_path，调用方好按固定路径找。
    downloaded = Path(result.artifacts[0]) if result.artifacts else None
    if not downloaded or not downloaded.exists():
        logger.warning("pixabay_bg_music: 检索成功但找不到下载文件")
        return None
    if downloaded != out_path:
        downloaded.replace(out_path)

    seconds = result.data.get("duration_seconds") or 0
    logger.info(f"pixabay_bg_music: 找到「{result.data.get('track_title', '?')}」"
                f"{seconds:.0f}s → {out_path}")
    return {"path": str(out_path), "seconds": float(seconds), "cost_usd": 0.0}
