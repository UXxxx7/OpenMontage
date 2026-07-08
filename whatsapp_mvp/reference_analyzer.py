# WhatsApp MVP - Reference (example video) Analyzer
# P2 Task 3: example video/screenshot -> contracts/style_params.schema.json
# (contract ③). Consumed by P1 (folded into the edit plan) and mapped by
# apply_style into render_props (contract ②) — but per contracts/README.md,
# this is AESTHETICS ONLY (palette/caption look/aspect/pacing), never
# objectPosition/scenes, which P2's apply_style computes from the SOURCE
# video's own face position, not the example's.
#
# Built on OpenMontage's existing analysis tools rather than a bespoke CV
# pipeline: ffprobe for aspect, frame_sampler for representative stills,
# scene_detect for cut frequency -> pacing.energy. Caption position and
# persistent-UI-bar detection are NOT reliably automatable from a handful of
# sampled frames without a dedicated model — per the task's own instruction,
# those get an honest default + low confidence rather than a guess dressed
# up as a real measurement.

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from PIL import Image

logger = logging.getLogger(__name__)

# Fields we do NOT have a reliable detector for yet — always emitted with a
# fixed default and a low confidence score so callers can see it's a guess.
_LOW_CONFIDENCE_DEFAULTS = {
    "captions": {"position": "bottom", "bottomRatio": 0.05, "karaoke": True},
    "chrome": {"chapterNav": True, "complianceBar": False, "brandBar": False, "progressBar": True},
}
_LOW_CONFIDENCE_SCORE = 0.3


def analyze_reference(source_path: str, workdir: Path) -> dict[str, Any]:
    """示例视频/截图 -> 契约③ style_params dict。

    source_path 可以是视频或单张图片（截图）——截图跳过 scene_detect（没有
    "剪辑节奏"这回事），其余步骤一样。
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"参考素材不存在: {source_path}")

    is_video = path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

    result: dict[str, Any] = {
        "source": {"kind": "video" if is_video else "screenshot", "ref": str(path)},
    }
    confidence: dict[str, float] = {}

    # --- aspect (ffprobe) ---
    w, h = _probe_dimensions(path)
    if w and h:
        result["aspect"] = _classify_aspect(w, h)
        confidence["aspect"] = 0.95

    # --- sample frames for color analysis ---
    frame_paths = _sample_frames(path, workdir, is_video)
    if frame_paths:
        color_mode, palette = _analyze_palette(frame_paths)
        result["colorMode"] = color_mode
        result["palette"] = palette
        confidence["colorMode"] = 0.75
        confidence["palette"] = 0.6
    else:
        logger.warning("reference_analyzer: 抽帧失败，colorMode/palette 用默认值")
        result["colorMode"] = "warm"
        confidence["colorMode"] = _LOW_CONFIDENCE_SCORE

    # --- pacing.energy (scene_detect — video only) ---
    if is_video:
        energy = _analyze_pacing(path, workdir)
        if energy is not None:
            result["pacing"] = {"energy": energy}
            confidence["pacing"] = 0.5  # cut frequency is a weak proxy for "energy"

    # --- fields we can't reliably detect: honest low-confidence defaults ---
    result["captions"] = dict(_LOW_CONFIDENCE_DEFAULTS["captions"])
    confidence["captions"] = _LOW_CONFIDENCE_SCORE
    result["chrome"] = dict(_LOW_CONFIDENCE_DEFAULTS["chrome"])
    confidence["chrome"] = _LOW_CONFIDENCE_SCORE

    result["confidence"] = confidence
    return result


# ---------------------------------------------------------------------------
# aspect
# ---------------------------------------------------------------------------

def _probe_dimensions(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        parts = [p for p in probe.stdout.strip().split(",") if p]
        w, h = parts[0], parts[1]
        return int(w), int(h)
    except Exception as e:
        logger.warning(f"reference_analyzer: ffprobe 取尺寸失败: {e}")
        return None, None


def _classify_aspect(w: int, h: int) -> str:
    ratio = w / h
    if ratio < 0.7:
        return "portrait"      # ~9:16
    if ratio < 1.2:
        return "square"        # ~1:1
    if ratio < 2.0:
        return "landscape"     # ~16:9
    return "cinematic"         # ~21:9


# ---------------------------------------------------------------------------
# frame sampling + palette / colorMode
# ---------------------------------------------------------------------------

def _sample_frames(path: Path, workdir: Path, is_video: bool, count: int = 8) -> list[Path]:
    if not is_video:
        return [path]

    out_dir = workdir / "reference_frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from tools.analysis.frame_sampler import FrameSampler
        r = FrameSampler().execute({
            "input_path": str(path), "strategy": "count", "count": count,
            "output_dir": str(out_dir), "format": "jpg",
        })
        if not r.success:
            logger.warning(f"reference_analyzer: frame_sampler 失败: {r.error}")
            return []
        return [Path(f["path"]) for f in r.data.get("frames", []) if Path(f["path"]).exists()]
    except Exception as e:
        logger.warning(f"reference_analyzer: frame_sampler 调用异常: {e}")
        return []


def _analyze_palette(frame_paths: list[Path]) -> tuple[str, dict[str, Any]]:
    """抽样帧 -> (colorMode, palette)。用 PIL 的调色板量化取每帧主色，
    再跨帧聚合——不依赖专门的颜色分析模型，MVP 够用。
    """
    all_colors: list[tuple[int, tuple[int, int, int]]] = []  # (count, rgb)
    for fp in frame_paths:
        try:
            img = Image.open(fp).convert("RGB")
            img.thumbnail((160, 160))  # downsample — palette doesn't need full res
            quantized = img.quantize(colors=6, method=Image.MEDIANCUT)
            palette = quantized.getpalette()
            counts = quantized.getcolors()  # [(count, index), ...]
            for count, idx in counts:
                rgb = tuple(palette[idx * 3: idx * 3 + 3])
                all_colors.append((count, rgb))
        except Exception as e:
            logger.warning(f"reference_analyzer: 读取帧 {fp} 失败: {e}")
            continue

    if not all_colors:
        return "warm", {}

    # Average brightness across all sampled swatches, weighted by pixel count.
    total_weight = sum(c for c, _ in all_colors)
    avg_brightness = sum(c * sum(rgb) / 3 for c, rgb in all_colors) / total_weight

    # Warm vs dark: primarily a brightness call (matches the two modes actually
    # documented for this template — cream-bright vs near-black), refined by
    # hue only as a tiebreaker near the midpoint.
    color_mode = "warm" if avg_brightness >= 110 else "dark"

    # Background guess: the single most common swatch (by weight).
    all_colors.sort(key=lambda c: -c[0])
    background = _rgb_to_hex(all_colors[0][1])

    # Primary/accent guess: most saturated swatches among the top 6, excluding
    # near-white/near-black (those are almost always background/text, not
    # brand accent color).
    def saturation(rgb: tuple[int, int, int]) -> float:
        mx, mn = max(rgb), min(rgb)
        return 0 if mx == 0 else (mx - mn) / mx

    vivid = [rgb for _, rgb in all_colors if 30 < sum(rgb) / 3 < 225]
    vivid.sort(key=saturation, reverse=True)
    primary = [_rgb_to_hex(c) for c in vivid[:2]] or [background]

    palette = {"primary": primary, "background": background}
    return color_mode, palette


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# ---------------------------------------------------------------------------
# pacing.energy
# ---------------------------------------------------------------------------

def _analyze_pacing(path: Path, workdir: Path) -> Optional[str]:
    try:
        from tools.analysis.scene_detect import SceneDetect
        out_json = workdir / "reference_scenes.json"
        r = SceneDetect().execute({"input_path": str(path), "output_path": str(out_json)})
        if not r.success:
            logger.warning(f"reference_analyzer: scene_detect 失败: {r.error}")
            return None
        scene_count = r.data.get("scene_count", 0)
        duration = _probe_duration(path)
        if duration <= 0:
            return None
        cuts_per_10s = scene_count / (duration / 10.0)
        if cuts_per_10s < 1.0:
            return "calm"
        if cuts_per_10s < 3.0:
            return "moderate"
        return "energetic"
    except Exception as e:
        logger.warning(f"reference_analyzer: scene_detect 调用异常: {e}")
        return None


def _probe_duration(path: Path) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(probe.stdout.strip())
    except Exception:
        return 0.0


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    wd = Path("/tmp/reference_analyzer_cli")
    wd.mkdir(parents=True, exist_ok=True)
    print(json.dumps(analyze_reference(src, wd), ensure_ascii=False, indent=2))
