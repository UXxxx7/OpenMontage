"""
pipeline/compose.py — Render Remotion overlay and composite with avatar via ffmpeg.

Two steps:
  render_overlay() — writes dynamic data.ts → runs `npx remotion render KnowledgeVideoV2`
  composite()       — ffmpeg overlays avatar video into the graphic overlay's video slot

The Remotion project at REMOTION_DIR is a shared resource. We write a temporary
data.ts file before rendering and restore the original afterwards (or just accept
the last render state — since wa-montage jobs are serial this is safe).
"""

import json
import os
import subprocess
import textwrap
from pathlib import Path

from pipeline.transcribe import words_to_subtitle_lines

FPS = int(os.environ.get("VIDEO_FPS", "25"))

# Where the my-video Remotion project lives
REMOTION_DIR = Path(os.environ.get("REMOTION_DIR", "/Users/liuyubo/Desktop/my-video"))
DATA_TS_PATH = REMOTION_DIR / "src/compositions/KnowledgeVideoV2/data.ts"

# Layout constants (must match index.tsx)
VIDEO_SLOT = {"x": 36, "y": 115, "w": 478, "h": 850}

# Phase stats keyed by phase id
PHASE_STATS = {
    "step1": {"value": 3,  "unit": "×", "label": "省時效果"},
    "step2": {"value": 95, "unit": "%", "label": "AI 製作率"},
    "step3": {"value": 30, "unit": "s", "label": "製作耗時"},
}

PHASE_ICONS = {
    "intro": "🎙️",
    "step1": "📱",
    "step2": "🤖",
    "step3": "📤",
    "cta":   "💬",
}

PHASE_STEP_NUMS = {
    "intro": "",
    "step1": "01",
    "step2": "02",
    "step3": "03",
    "cta":   "",
}


# ── Public API ─────────────────────────────────────────────────────────────────

def render_overlay(script: dict, transcript: dict, project_dir: Path) -> str:
    """Write dynamic data.ts and render the Remotion overlay.  Returns local path."""
    out_path = project_dir / "overlay.mp4"
    if out_path.exists():
        return str(out_path)

    total_frames = max(round(transcript["duration"] * FPS), 1)
    phases        = _build_phases(script, transcript, total_frames)
    subtitles     = words_to_subtitle_lines(transcript["words"])
    flow_nodes    = _build_flow_nodes(phases)

    data_ts = _generate_data_ts(total_frames, phases, subtitles, flow_nodes)

    original_data = DATA_TS_PATH.read_text() if DATA_TS_PATH.exists() else None
    try:
        DATA_TS_PATH.write_text(data_ts)
        subprocess.run(
            [
                "npx", "remotion", "render",
                "KnowledgeVideoV2",
                str(out_path),
                "--duration", str(total_frames),
                "--concurrency", "4",
            ],
            cwd=str(REMOTION_DIR),
            check=True,
        )
    finally:
        if original_data is not None:
            DATA_TS_PATH.write_text(original_data)

    return str(out_path)


def composite(avatar_path: str, overlay_path: str, project_dir: Path) -> str:
    """Composite avatar video into the graphic overlay's video slot."""
    final_path = project_dir / "final.mp4"
    if final_path.exists():
        return str(final_path)

    x = VIDEO_SLOT["x"]
    y = VIDEO_SLOT["y"]
    w = VIDEO_SLOT["w"]
    h = VIDEO_SLOT["h"]

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(overlay_path),
            "-i", str(avatar_path),
            "-filter_complex",
            f"[1:v]scale={w}:{h}[av];[0:v][av]overlay={x}:{y}",
            "-map", "0:a?",         # keep overlay audio (bgmusic) if present
            "-map", "1:a?",         # keep avatar audio too
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(final_path),
        ],
        check=True,
    )
    return str(final_path)


# ── Frame layout helpers ───────────────────────────────────────────────────────

def _build_phases(script: dict, transcript: dict, total_frames: int) -> list[dict]:
    """
    Assign frame boundaries to each phase proportional to character count.
    Phases are: intro → step1 → step2 → step3 → cta
    """
    phase_ids  = ["intro", "step1", "step2", "step3", "cta"]
    phase_meta = {p["id"]: p for p in script.get("phases", [])}
    lines      = script.get("lines", [])

    # Character count per phase (for proportional frame allocation)
    char_counts: dict[str, int] = {pid: 0 for pid in phase_ids}
    for line in lines:
        pid = line.get("phase", "intro")
        if pid in char_counts:
            char_counts[pid] += len(line.get("text", ""))

    total_chars = sum(char_counts.values()) or 1

    # Build boundaries
    phases_out = []
    current_sf = 0
    for pid in phase_ids:
        fraction = char_counts[pid] / total_chars
        duration = round(fraction * total_frames)
        ef       = min(current_sf + duration, total_frames)

        meta = phase_meta.get(pid, {})
        stat = PHASE_STATS.get(pid)
        phases_out.append({
            "id":          pid,
            "sf":          current_sf,
            "ef":          ef,
            "stepNum":     PHASE_STEP_NUMS.get(pid, ""),
            "title":       meta.get("title", pid),
            "subtitle":    meta.get("subtitle", ""),
            "body":        meta.get("body", ""),
            "icon":        meta.get("icon", PHASE_ICONS.get(pid, "🎬")),
            "accentFrame": current_sf,
            "stat":        stat,
        })
        current_sf = ef

    # Ensure the last phase ends exactly at total_frames
    if phases_out:
        phases_out[-1]["ef"] = total_frames

    return phases_out


def _build_flow_nodes(phases: list[dict]) -> list[dict]:
    """Build FLOW_NODES that animate in during step2."""
    step2 = next((p for p in phases if p["id"] == "step2"), None)
    if not step2:
        return []
    s = step2["sf"]
    offsets = [16, 56, 91, 126]
    nodes   = [
        {"icon": "📱", "label": "WhatsApp"},
        {"icon": "🤖", "label": "AI Agent"},
        {"icon": "🎬", "label": "數字人"},
        {"icon": "📤", "label": "上傳"},
    ]
    return [{**n, "sf": s + o} for n, o in zip(nodes, offsets)]


# ── TypeScript code generation ─────────────────────────────────────────────────

def _ts_str(s: str) -> str:
    """Escape a Python string for TypeScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _generate_data_ts(
    total_frames: int,
    phases: list[dict],
    subtitles: list[dict],
    flow_nodes: list[dict],
) -> str:
    lines = [
        "// Auto-generated by wa-montage pipeline/compose.py — do not edit manually",
        "",
        f"export const FPS = {FPS};",
        f"export const TOTAL_FRAMES = {total_frames};",
        "",
        "export interface Word   {{ text: string; sf: number; ef: number }}",
        "export interface SubLine {{ words: Word[]; sf: number; ef: number }}",
        "",
        "export const SUBTITLES: SubLine[] = [",
    ]
    # Subtitles
    for sub in subtitles:
        words_ts = ", ".join(
            f'{{ text: "{_ts_str(w["text"])}", sf: {w["sf"]}, ef: {w["ef"]} }}'
            for w in sub["words"]
        )
        lines.append(
            f'  {{ sf: {sub["sf"]}, ef: {sub["ef"]}, words: [{words_ts}] }},'
        )
    lines += [
        "];",
        "",
        "export interface Stat { value: number; unit: string; label: string }",
        "",
        "export const PHASES = [",
    ]
    # Phases
    for p in phases:
        stat_ts = (
            f'{{ value: {p["stat"]["value"]}, unit: "{_ts_str(p["stat"]["unit"])}", '
            f'label: "{_ts_str(p["stat"]["label"])}" }} as Stat'
            if p["stat"] else "null as Stat | null"
        )
        lines += [
            "  {",
            f'    id: "{p["id"]}",',
            f'    sf: {p["sf"]}, ef: {p["ef"]},',
            f'    stepNum: "{p["stepNum"]}",',
            f'    title: "{_ts_str(p["title"])}",',
            f'    subtitle: "{_ts_str(p["subtitle"])}",',
            f'    body: "{_ts_str(p["body"])}",',
            f'    icon: "{p["icon"]}",',
            f'    accentFrame: {p["accentFrame"]},',
            f'    stat: {stat_ts},',
            "  },",
        ]
    lines += [
        "];",
        "",
        "export const FLOW_NODES = [",
    ]
    # Flow nodes
    for n in flow_nodes:
        lines.append(
            f'  {{ icon: "{n["icon"]}", label: "{_ts_str(n["label"])}", sf: {n["sf"]} }},'
        )
    lines += ["];", ""]
    return "\n".join(lines)
