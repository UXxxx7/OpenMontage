"""
pipeline/transcribe.py — Extract word-level timestamps from avatar video.

Uses OpenAI Whisper API (cheapest, no local GPU needed).
Falls back to local whisper CLI if OPENAI_API_KEY is not set.

Output: transcript.json saved to project_dir, returned as dict.
Schema:
{
  "words": [
    { "text": "大家好", "start": 0.48, "end": 0.92 },
    ...
  ],
  "segments": [
    { "text": "大家好，我係 Dickson", "start": 0.48, "end": 2.10 },
    ...
  ],
  "duration": 38.5
}
"""

import json
import os
import subprocess
from pathlib import Path

import requests

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
FPS = int(os.environ.get("VIDEO_FPS", "25"))


def transcribe(video_path: str, project_dir: Path) -> dict:
    """Transcribe avatar video, return word-level timestamp dict."""
    out_path = project_dir / "transcript.json"
    if out_path.exists():
        return json.loads(out_path.read_text())

    if OPENAI_KEY:
        result = _transcribe_openai(video_path)
    else:
        result = _transcribe_local(video_path)

    # Attach frame numbers (useful downstream for Remotion data)
    for w in result.get("words", []):
        w["sf"] = int(w["start"] * FPS)
        w["ef"] = int(w["end"]   * FPS)

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _transcribe_openai(video_path: str) -> dict:
    """Use OpenAI Whisper API with word timestamps."""
    with open(video_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            files={"file": (Path(video_path).name, f, "video/mp4")},
            data={
                "model": "whisper-1",
                "language": "zh",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            timeout=120,
        )
    resp.raise_for_status()
    data = resp.json()

    words = [
        {"text": w["word"].strip(), "start": w["start"], "end": w["end"]}
        for w in data.get("words", [])
    ]
    segments = [
        {"text": s["text"].strip(), "start": s["start"], "end": s["end"]}
        for s in data.get("segments", [])
    ]

    return {
        "words": words,
        "segments": segments,
        "duration": data.get("duration", 0),
    }


def _transcribe_local(video_path: str) -> dict:
    """Fallback: use local whisper CLI."""
    out_dir = Path(video_path).parent
    subprocess.run(
        ["whisper", video_path, "--language", "zh",
         "--output_format", "json", "--output_dir", str(out_dir)],
        check=True,
        capture_output=True,
    )
    stem = Path(video_path).stem
    raw = json.loads((out_dir / f"{stem}.json").read_text())

    words = []
    for seg in raw.get("segments", []):
        for w in seg.get("words", []):
            words.append({
                "text": w["word"].strip(),
                "start": w["start"],
                "end": w["end"],
            })

    return {
        "words": words,
        "segments": [
            {"text": s["text"], "start": s["start"], "end": s["end"]}
            for s in raw.get("segments", [])
        ],
        "duration": raw.get("segments", [{}])[-1].get("end", 0),
    }


def words_to_subtitle_lines(words: list[dict], max_words_per_line: int = 5) -> list[dict]:
    """
    Group word timestamps into subtitle lines for Remotion SubtitleBar.
    Returns list of { sf, ef, words: [{text, sf, ef}] }
    """
    lines = []
    i = 0
    while i < len(words):
        chunk = words[i: i + max_words_per_line]
        lines.append({
            "sf": chunk[0]["sf"],
            "ef": chunk[-1]["ef"],
            "words": [{"text": w["text"], "sf": w["sf"], "ef": w["ef"]} for w in chunk],
        })
        i += max_words_per_line
    return lines
