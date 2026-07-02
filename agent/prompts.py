"""
prompts.py — Builds the Claude system prompt from OpenMontage's skill files.

Strategy: load AGENT_GUIDE.md as the base, then inject the stage-director
skills for the chosen pipeline. We keep context lean by only loading what's
needed for the current pipeline (not all 400+ skills at once).
"""

import os
import re
from pathlib import Path

OPEN_MONTAGE_PATH = Path(
    os.environ.get("OPEN_MONTAGE_PATH", str(Path(__file__).resolve().parents[2] / "OpenMontage"))
)

# Maps pipeline name → list of stage skill paths to pre-load
PIPELINE_SKILLS: dict[str, list[str]] = {
    "animated-explainer": [
        "skills/pipelines/explainer/research-director.md",
        "skills/pipelines/explainer/proposal-director.md",
        "skills/pipelines/explainer/script-director.md",
        "skills/pipelines/explainer/scene-plan-director.md",
        "skills/pipelines/explainer/assets-director.md",
        "skills/pipelines/explainer/edit-director.md",
        "skills/pipelines/explainer/compose-director.md",
    ],
    "avatar-spokesperson": [
        "skills/pipelines/avatar-spokesperson/script-director.md",
        "skills/pipelines/avatar-spokesperson/assets-director.md",
        "skills/pipelines/avatar-spokesperson/compose-director.md",
    ],
    "talking-head": [
        "skills/pipelines/talking-head/edit-director.md",
        "skills/pipelines/talking-head/compose-director.md",
    ],
    "animation": [
        "skills/pipelines/animation/script-director.md",
        "skills/pipelines/animation/scene-plan-director.md",
        "skills/pipelines/animation/assets-director.md",
        "skills/pipelines/animation/compose-director.md",
    ],
    "cinematic": [
        "skills/pipelines/cinematic/script-director.md",
        "skills/pipelines/cinematic/scene-plan-director.md",
        "skills/pipelines/cinematic/assets-director.md",
        "skills/pipelines/cinematic/compose-director.md",
    ],
}

# WhatsApp-specific addendum injected at the top of every system prompt
_WA_ADDENDUM = """
## WhatsApp Deployment Context

You are running as a headless agent inside a WhatsApp production pipeline.
The user communicates with you via WhatsApp messages — there is no IDE,
no file browser, and no interactive terminal.

Operational rules:
- NEVER ask the user to run shell commands or open files.
- Keep text responses SHORT (≤ 3 sentences). Long prose becomes unreadable in WhatsApp.
- When you need user approval, present options as a numbered list (1. / 2. / 3.)
  and ask the user to reply with the number. Nothing else.
- After every major stage (script done, visuals done, composing…) send a brief
  progress line so the user knows you are still working.
- The final deliverable is always a video file. Always call video_compose (or
  video_stitch) to produce renders/final.mp4 before finishing.
- You may NOT ask the user to confirm trivial technical choices
  (codec, CRF, resolution). Make sensible defaults and proceed.

""".strip()


def _read(rel_path: str) -> str:
    full = OPEN_MONTAGE_PATH / rel_path
    if full.exists():
        return full.read_text(encoding="utf-8")
    return f"<!-- skill file not found: {rel_path} -->"


def build_system_prompt(pipeline_name: str) -> str:
    parts: list[str] = []

    # 1. WhatsApp deployment rules (highest priority)
    parts.append(_WA_ADDENDUM)

    # 2. OpenMontage agent guide
    agent_guide = _read("AGENT_GUIDE.md")
    # Truncate to keep context manageable (~120 KB cap for system prompt)
    parts.append(agent_guide[:60_000])

    # 3. Pipeline manifest (YAML — gives stage order and quality gates)
    manifest = _read(f"pipeline_defs/{pipeline_name}.yaml")
    parts.append(f"## Active Pipeline Manifest\n\n```yaml\n{manifest}\n```")

    # 4. Stage director skills for this pipeline
    skill_paths = PIPELINE_SKILLS.get(pipeline_name, [])
    for path in skill_paths:
        content = _read(path)
        parts.append(f"## Skill: {path}\n\n{content}")

    return "\n\n---\n\n".join(parts)


# ── Pipeline selection heuristic ─────────────────────────────────────
_PIPELINE_KEYWORDS: list[tuple[list[str], str]] = [
    (["avatar", "spokesperson", "presenter", "talking head", "heygen", "数字人", "數字人"], "avatar-spokesperson"),
    (["animation", "motion graphic", "kinetic", "animated", "動畫", "动画"], "animation"),
    (["cinematic", "trailer", "teaser", "sci-fi", "drama", "電影", "电影"], "cinematic"),
    (["talking head", "vlog", "footage", "interview", "直播", "講解"], "talking-head"),
]

def select_pipeline(prompt: str) -> str:
    lower = prompt.lower()
    for keywords, pipeline in _PIPELINE_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return pipeline
    return "animated-explainer"   # safe default
