"""渲染前 QA stills——video-studio 流程的机器可查部分。

video-studio 的质量来源之一是"渲染整片之前先抽帧看"（CLAUDE-v2 §3/§8：
`npx remotion still --scale=0.5` 逐 beat 抽查，比整片渲染便宜两个数量级）。
WhatsApp 自动管线里没有人眼，这个模块把清单里**机器能查的部分**自动化：

- 选帧：intro 落位后、每张数据卡完全展开后、卡片全屏区间中点、片尾——
  正是 codex 说的 "intro beat / first data display frame / section midpoints"。
- 检查：停靠模式下内容区的填充率（空画布检测——参考构建的核心规则是
  "卡片缩小必须是为了给内容让位"，缩了却没内容 = 布局 bug）。

查不了的部分（脸的位置对不对、图形跟口播语义配不配）需要有视觉能力的
agent 看 stills 决定——所以这里把 stills 路径 + 结构化 findings 一起返回，
P1 的 L2 agent 以后可以直接消费。找不到 npx / 组合没注册时整体跳过并返回
空结果，绝不让 QA 环节本身搞垮渲染。
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from .llm_client import call_llm_vision

logger = logging.getLogger(__name__)

_FPS = 30
_SCALE = 0.5
_STILL_TIMEOUT_S = 120

# Must match pipeline_runner's geometry (single source of truth would be a
# circular import; these mirror _DOMINANT_BOX/_WORKFLOW_BOX, change together).
# Workflow mode: card docks small at bottom-right (740,1200 300x531); the
# graphics (dataCards/gauges/countdowns/calendars, default y=900) occupy the
# freed canvas — the fill check samples that region.
_WORKFLOW_BOX_H = 531
_CONTENT_ZONE = {"x0": 60, "y0": 860, "x1": 1020, "y1": 1190}
_BG = {"warm": (0xF2, 0xEB, 0xE0), "dark": (0x0D, 0x11, 0x17)}
_MIN_CONTENT_FILL = 0.06  # below this, a docked frame's content zone is "empty canvas"


def _card_h_at(scenes: list[dict], frame: int) -> float:
    """SpeakerCard 同款线性关键帧插值（easing 不改变端点值，对停留区间取值精确）。"""
    if not scenes:
        return 0
    prev = scenes[0]
    if frame <= prev["frame"]:
        return prev["h"]
    for s in scenes[1:]:
        if frame <= s["frame"]:
            span = s["frame"] - prev["frame"]
            t = (frame - prev["frame"]) / span if span else 1.0
            return prev["h"] + (s["h"] - prev["h"]) * t
        prev = s
    return prev["h"]


def is_docked(scenes: list[dict], frame: int) -> bool:
    return abs(_card_h_at(scenes, frame) - _WORKFLOW_BOX_H) < 1


def pick_qa_frames(props: dict) -> list[int]:
    """codex 的抽查点：intro 落位、每张数据卡全展开、全屏区间中点、片尾。"""
    duration_frames = max(1, round(props["durationSeconds"] * _FPS))
    frames = {min(props.get("introOutFrame", 20) + 15, duration_frames - 1)}
    for card in props.get("dataCards", []):
        last_row = max((r.get("mountOffset", 0) for r in card.get("rows", [])), default=0)
        frames.add(min(card["mountFrame"] + last_row + 30, duration_frames - 1))
    frames.add(max(duration_frames - 30, 0))
    scenes = props.get("scenes", [])
    full_frames = [f for f in range(0, duration_frames, 30) if not is_docked(scenes, f)]
    if full_frames:
        frames.add(full_frames[len(full_frames) // 2])
    return sorted(f for f in frames if 0 <= f < duration_frames)


def render_still(remotion_dir: Path, props_path: Path, frame: int, out_png: Path) -> bool:
    cmd = [
        "npx", "remotion", "still", "XiaojinEditorial", str(out_png),
        f"--frame={frame}", f"--props={props_path}", f"--scale={_SCALE}",
    ]
    try:
        r = subprocess.run(cmd, cwd=remotion_dir, capture_output=True, text=True, timeout=_STILL_TIMEOUT_S)
    except Exception as e:
        logger.warning(f"  qa_stills: still f{frame} 渲染异常: {e}")
        return False
    if r.returncode != 0:
        logger.warning(f"  qa_stills: still f{frame} 渲染失败: {(r.stderr or '').strip()[-300:]}")
        return False
    return out_png.exists()


def check_content_fill(png_path: Path, props: dict, frame: int) -> Optional[dict]:
    """停靠帧的内容区填充率检查。返回 finding dict 或 None（通过/不适用）。"""
    scenes = props.get("scenes", [])
    if not is_docked(scenes, frame):
        return None  # full-screen frame — the card itself fills the canvas
    try:
        from PIL import Image
    except ImportError:
        return None

    bg = _BG.get(props.get("colorMode", "warm"), _BG["warm"])
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        zone = im.crop((
            round(_CONTENT_ZONE["x0"] * _SCALE), round(_CONTENT_ZONE["y0"] * _SCALE),
            round(_CONTENT_ZONE["x1"] * _SCALE), round(_CONTENT_ZONE["y1"] * _SCALE),
        ))
        px = list(zone.getdata())
    if not px:
        return None
    non_bg = sum(1 for (r, g, b) in px if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 45)
    fill = non_bg / len(px)
    if fill < _MIN_CONTENT_FILL:
        return {
            "check": "empty_canvas",
            "frame": frame,
            "fill_ratio": round(fill, 4),
            "detail": "卡片处于停靠(缩小)状态但内容区基本是空的——缩小必须是为了给内容让位",
        }
    return None


VISION_SYSTEM_PROMPT = """You are doing a final visual quality check on rendered
frames from an automated talking-head video editor, before delivery to a real user.
You will see one or more still frames, each preceded by a text label giving its
frame number. For EACH frame, check only what a single static image can show:

1. Speaker visibility: if a speaker/face card is shown, is the face (ideally chest/
   shoulders too) actually visible — not cropped out, cut off, or fully obscured?
2. Overlap: does any card/caption/chip/graphic visibly cover another piece of
   content it shouldn't (e.g. a caption sitting on top of a data card's numbers,
   two cards stacked on each other)?
3. Completeness: does the frame look like a finished, intentional shot — not a
   blank/empty canvas, not an obviously broken or half-rendered layout?

Do NOT judge anything needing motion, audio, or multi-frame comparison (timing,
animation smoothness, caption sync) — you only have static images.

For each frame with a genuine problem, classify severity:
- "major": the frame is actually broken (face cut off / cards unreadable due to
  overlap / blank canvas where content should be)
- "minor": a small aesthetic issue that doesn't make the frame unusable

Output ONLY valid JSON, no markdown, no prose:
{"findings": [{"frame": <number>, "issue": "short description", "severity": "major"|"minor"}]}
If everything looks fine, return {"findings": []}."""


def review_stills(stills: list[dict]) -> list[dict]:
    """把已经渲染好的 stills 一次性丢给视觉 LLM 复核——video-studio CLAUDE-v2 §9
    "score before you ship" 人工步骤里，机器测不了的那部分（"脸的位置对不对"、
    "东西有没有叠在一起"）。跟 check_content_fill 那种像素统计不是一回事：那个
    只能测"内容区是不是空的"，测不了语义层面的重叠/取景。

    没配 VISION_LLM_API_KEY、调用失败、或解析失败时返回空列表——按这个文件
    其余可选精修步骤同款的 best-effort 哲学，绝不让这一步搞垮渲染。findings
    的 "check" 字段固定为 "vision_review"，方便调用方跟 check_content_fill
    的 findings 区分开、只对这一类做重试判断。
    """
    if not stills:
        return []
    images = [(f"Frame {s['frame']}:", Path(s["path"])) for s in stills]
    raw = call_llm_vision(VISION_SYSTEM_PROMPT, "Review all frames per the rules above.", images, temperature=0.1)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning(f"  qa_stills: 视觉复核结果解析失败，跳过: {e}")
        return []
    out = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict) or "frame" not in f:
            continue
        out.append({
            "check": "vision_review",
            "frame": f["frame"],
            "issue": str(f.get("issue", ""))[:200],
            "severity": f.get("severity") if f.get("severity") in ("major", "minor") else "minor",
        })
    return out


def run_props_qa(props: dict, props_path: Path, remotion_dir: Path, out_dir: Path) -> dict:
    """渲 QA stills + 跑机器检查 + 视觉复核。永不 raise；渲染环境不可用时返回空结果。

    返回 {"stills": [{"frame", "path"}...], "findings": [finding...]}。
    """
    result: dict = {"stills": [], "findings": []}
    if not (remotion_dir / "package.json").exists():
        logger.info("  qa_stills: remotion-composer 不可用，跳过 QA stills")
        return result
    out_dir.mkdir(parents=True, exist_ok=True)

    for frame in pick_qa_frames(props):
        png = out_dir / f"qa_f{frame}.png"
        if not render_still(remotion_dir, props_path, frame, png):
            result["findings"].append({"check": "still_render_failed", "frame": frame})
            continue
        result["stills"].append({"frame": frame, "path": str(png)})
        finding = check_content_fill(png, props, frame)
        if finding:
            result["findings"].append(finding)

    if result["stills"]:
        result["findings"].extend(review_stills(result["stills"]))

    (out_dir / "qa_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for f in result["findings"]:
        logger.warning(f"  qa_stills 发现问题: {f}")
    return result
