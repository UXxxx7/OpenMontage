"""
service.py — Deterministic video production service.

Fixed pipeline (no LLM orchestration overhead):
  1. script     — LLM generates structured Cantonese script
  2. avatar     — HeyGen renders avatar video from script
  3. transcribe — WhisperX extracts word-level timestamps
  4. compose    — Remotion overlay + ffmpeg composite → final.mp4

Each step is independently resumable via a checkpoint file.
Progress events are published to Redis so the Node worker can relay
them to WhatsApp in real time.
"""

import json
import os
import sys
from pathlib import Path

from state import JobState
from whatsapp import notify_text, notify_video
from pipeline.script import generate_script
from pipeline.avatar import generate_avatar
from pipeline.transcribe import transcribe
from pipeline.compose import render_overlay, composite


class VideoService:
    def __init__(self, state: JobState) -> None:
        self.state = state
        self.project = Path(state.project_dir)
        self.checkpoint_file = self.project / "checkpoint.json"

    # ── Public entry point ────────────────────────────────────────────
    def run(self, prompt: str) -> str:
        """
        Run the full pipeline for a user prompt.
        Returns the path to the final video file.
        """
        cp = self._load_checkpoint()

        # ── Step 1: Script ────────────────────────────────────────────
        if "script" not in cp:
            notify_text(self.state, "📝 正在生成腳本…")
            script = generate_script(prompt, self.project)
            cp["script"] = script
            self._save_checkpoint(cp)
            notify_text(self.state, f"✅ 腳本完成（{len(script['lines'])} 句）")
        else:
            script = cp["script"]

        # ── Step 2: Avatar video ──────────────────────────────────────
        if "avatar_path" not in cp:
            notify_text(self.state, "🎬 正在生成數字人視頻（約 3-5 分鐘）…")
            avatar_path = generate_avatar(script, self.project)
            cp["avatar_path"] = avatar_path
            self._save_checkpoint(cp)
            notify_text(self.state, "✅ 數字人視頻生成完畢")
        else:
            avatar_path = cp["avatar_path"]

        # ── Step 3: Transcribe ────────────────────────────────────────
        if "transcript" not in cp:
            notify_text(self.state, "🔤 正在提取字幕時間戳…")
            transcript = transcribe(avatar_path, self.project)
            cp["transcript"] = transcript
            self._save_checkpoint(cp)
        else:
            transcript = cp["transcript"]

        # ── Step 4: Overlay + Composite ───────────────────────────────
        if "final_path" not in cp:
            notify_text(self.state, "🎨 正在渲染動效疊加層…")
            overlay_path = render_overlay(script, transcript, self.project)

            notify_text(self.state, "⚙️ 正在合成最終視頻…")
            final_path = composite(avatar_path, overlay_path, self.project)

            cp["final_path"] = final_path
            self._save_checkpoint(cp)
        else:
            final_path = cp["final_path"]

        # ── Done ──────────────────────────────────────────────────────
        self.state.set_final_video(final_path)
        notify_video(self.state, final_path, "🎉 你的視頻製作完成！")
        return final_path

    # ── Checkpoint helpers ────────────────────────────────────────────
    def _load_checkpoint(self) -> dict:
        if self.checkpoint_file.exists():
            return json.loads(self.checkpoint_file.read_text())
        return {}

    def _save_checkpoint(self, data: dict) -> None:
        self.checkpoint_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
