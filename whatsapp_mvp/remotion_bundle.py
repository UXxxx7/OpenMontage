# WhatsApp MVP - Remotion 预打包缓存
#
# `npx remotion still/render` 每次调用都会现场 bundle 整个工程（实测 20-40s）。
# 一个任务的 QA stills（5-6 张，视觉重试后 8-12 张）+ 整片渲染 = 同一份代码
# 被反复打包十来次，占掉任务总时长的一大块；两任务并发时这些打包还要过
# RENDER_SLOTS 闸门排队，互相放大等待。
#
# 解法：`npx remotion bundle` 预打包到 build/，之后所有 still/render 直接吃
# bundle 目录，跳过打包。src/ 有改动（mtime 更新）时自动重新打包。打包失败
# 返回 None，调用方回退到原始的按次打包路径——这只是加速器，不是新依赖。

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BUNDLE_LOCK = threading.Lock()
_BUNDLE_TIMEOUT_S = 300


def _src_mtime(remotion_dir: Path) -> float:
    newest = 0.0
    for sub in ("src", "contracts"):
        base = remotion_dir / sub if sub == "src" else remotion_dir.parent / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
    return newest


def ensure_remotion_bundle(remotion_dir: Path) -> Optional[str]:
    """返回可直接喂给 still/render 的 bundle 目录；不可用时返回 None。"""
    remotion_dir = Path(remotion_dir).resolve()  # 相对路径+cwd 组合会把 out-dir 解析进嵌套目录（实测）
    build = remotion_dir / "build"
    marker = build / "index.html"
    src_time = _src_mtime(remotion_dir)

    if marker.exists() and marker.stat().st_mtime >= src_time:
        return str(build)

    with _BUNDLE_LOCK:
        if marker.exists() and marker.stat().st_mtime >= src_time:
            return str(build)
        npx = shutil.which("npx") or "npx"
        logger.info("  remotion: 预打包 bundle（src 有更新或首次）...")
        try:
            r = subprocess.run(
                [npx, "remotion", "bundle", "--out-dir", str(build)],
                cwd=remotion_dir, capture_output=True, text=True,
                timeout=_BUNDLE_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning(f"  remotion: bundle 失败（回退按次打包）: {e}")
            return None
        if r.returncode != 0 or not marker.exists():
            logger.warning(f"  remotion: bundle 失败（回退按次打包）: {(r.stderr or '')[-300:]}")
            return None
        logger.info("  remotion: bundle 就绪")
        return str(build)
