#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2 · RenderQA —— 成片 lint(props_lint 的"输出侧"对称物,设计文档 §2.5)。

对渲出来的 mp4 跑一组**只看像素/音频、不需语义**的廉价探针(纯 ffmpeg/ffprobe,秒级),
产出 QAVerdict 驱动 ComposeOrchestrator:accept → 导出;revise → 喂 FeedbackReviser。

探针(§4 QAVerdict.kind):
  black    —— 大面积接近黑的区段(blackframe;实测已在真实黑屏片上验证阈值)
  freeze   —— 画面长时间完全静止(freezedetect;讲话人视频不该有 ≥FREEZE_MIN_S 的死帧)
  no_audio —— 音轨缺失(ffprobe streams)
  duration —— 成片时长与期望差超容差

明确不做:字幕内容/语义判断(需 OCR,不是廉价探针;"代码消费 words"由 M1 静态闸保证)。

verdict 规则:任一 blocking 缺陷 → revise;否则 accept。
score = 1 − 缺陷覆盖时长占比,仅用于灰度对比与"接受当前最好版"时择优(§2.5)。
每个缺陷区段抽一帧中点证据图(可直接作为 FeedbackReviser 的证据帧)。

用法:
    from render_qa import qa_render
    v = qa_render(Path("out.mp4"), expected_duration_s=27.6)
    v.verdict  # "accept" | "revise"
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ─────────────────────────── 阈值(与实测对齐,均可配)───────────────────────────

BLACK_PCT_MIN = 30      # 整帧接近黑的像素占比 ≥30% 才算(实测:黑屏卡片 40%,b-roll 深色屏 17-19%)
BLACK_MIN_S = 1.2       # 黑区段最短持续
BLACK_THRESH = 32       # 像素亮度 ≤32/255 视作"接近黑"
SAMPLE_FPS = 2          # 黑屏探针抽样帧率(降本)

FREEZE_NOISE_DB = -60   # freezedetect 噪声容限:-60dB≈逐像素几乎全同才算冻结(讲话人有呼吸/噪点,不会误报)
FREEZE_MIN_S = 4.0      # 冻结最短持续:b-roll 屏录可能短暂静止,≥4s 才算缺陷

DURATION_TOL_S = 1.5    # 时长容差


@dataclass
class Defect:
    t0: float
    t1: float
    kind: str            # black | freeze | no_audio | duration
    desc: str
    frame_path: str | None = None


@dataclass
class QAVerdict:
    verdict: str                       # accept | revise
    score: float                       # 1 − 缺陷覆盖时长占比(0–1)
    defects: list = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "score": round(self.score, 3),
                "duration_s": round(self.duration_s, 2),
                "defects": [asdict(d) for d in self.defects]}


# ─────────────────────────── ffmpeg/ffprobe 基元 ───────────────────────────

def _run(cmd: list, timeout_s: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout_s)


def _probe_duration(mp4: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", str(mp4)], 30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _has_audio(mp4: Path) -> bool:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(mp4)], 30)
    return "audio" in r.stdout


def _grab_frame(mp4: Path, t: float, out: Path) -> str | None:
    _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4),
          "-frames:v", "1", "-vf", "scale=540:-1", str(out)], 30)
    return str(out) if out.exists() else None


# ─────────────────────────── 探针 ───────────────────────────

def _probe_black(mp4: Path) -> list[tuple[float, float]]:
    """blackframe 逐帧 pblack(%),聚合成 ≥BLACK_MIN_S 的区段。(M1 阶段已在真实
    黑屏片上验证:黑卡片段 pblack=40 稳定命中,b-roll 深色屏 17-19 不误报。)"""
    r = _run(["ffmpeg", "-v", "info", "-i", str(mp4),
              "-vf", f"fps={SAMPLE_FPS},blackframe=amount=0:thresh={BLACK_THRESH}",
              "-an", "-f", "null", "-"])
    rows = sorted((float(m.group(2)), int(m.group(1)))
                  for m in re.finditer(r"pblack:(\d+)\s+pts:\d+\s+t:([0-9.]+)", r.stderr))
    runs, run = [], None
    step = 1.0 / SAMPLE_FPS
    for t, pb in rows:
        if pb >= BLACK_PCT_MIN:
            run = [t, t] if run is None else [run[0], t]
        else:
            if run and (run[1] - run[0] + step) >= BLACK_MIN_S:
                runs.append((run[0], run[1] + step))
            run = None
    if run and (run[1] - run[0] + step) >= BLACK_MIN_S:
        runs.append((run[0], run[1] + step))
    return runs


def _probe_freeze(mp4: Path) -> list[tuple[float, float]]:
    """freezedetect:画面完全静止 ≥FREEZE_MIN_S 的区段。"""
    r = _run(["ffmpeg", "-v", "info", "-i", str(mp4),
              "-vf", f"freezedetect=n={FREEZE_NOISE_DB}dB:d={FREEZE_MIN_S}",
              "-an", "-f", "null", "-"])
    starts = [float(m.group(1)) for m in re.finditer(r"freeze_start:\s*([0-9.]+)", r.stderr)]
    ends = [float(m.group(1)) for m in re.finditer(r"freeze_end:\s*([0-9.]+)", r.stderr)]
    runs = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else _probe_duration(mp4)  # 冻到片尾则无 freeze_end
        runs.append((s, e))
    return runs


# ─────────────────────────── 入口 ───────────────────────────

def qa_render(mp4: Path,
              expected_duration_s: float | None = None,
              expect_audio: bool = True,
              evidence_dir: Path | None = None) -> QAVerdict:
    mp4 = Path(mp4)
    if not mp4.exists():
        return QAVerdict("revise", 0.0,
                         [Defect(0, 0, "duration", f"成片不存在:{mp4}")], 0.0)
    dur = _probe_duration(mp4)
    defects: list[Defect] = []

    if dur <= 0:
        return QAVerdict("revise", 0.0,
                         [Defect(0, 0, "duration", "读不出成片时长(文件损坏?)")], 0.0)

    # duration
    if expected_duration_s and abs(dur - expected_duration_s) > DURATION_TOL_S:
        defects.append(Defect(0, dur, "duration",
                              f"时长 {dur:.1f}s 与期望 {expected_duration_s:.1f}s 差超 {DURATION_TOL_S}s"))

    # no_audio
    if expect_audio and not _has_audio(mp4):
        defects.append(Defect(0, dur, "no_audio", "成片没有音轨(讲话人声音丢失)"))

    # black
    for t0, t1 in _probe_black(mp4):
        defects.append(Defect(t0, t1, "black",
                              f"{t0:.1f}-{t1:.1f}s 大面积接近黑({t1 - t0:.1f}s)——"
                              f"某元素多半只渲出了黑色背景,该区间应有实际内容"))

    # freeze
    for t0, t1 in _probe_freeze(mp4):
        defects.append(Defect(t0, t1, "freeze",
                              f"{t0:.1f}-{t1:.1f}s 画面完全静止({t1 - t0:.1f}s)——"
                              f"视频轨可能没被驱动或被静态元素完全遮挡"))

    # 证据帧(区段中点)
    if evidence_dir:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(defects):
            if d.kind in ("black", "freeze") and d.t1 > d.t0:
                d.frame_path = _grab_frame(mp4, (d.t0 + d.t1) / 2,
                                           evidence_dir / f"qa_{d.kind}_{i}.png")

    # score:1 − 缺陷覆盖时长占比(区段并集;no_audio/duration 记全片)
    covered: list[tuple[float, float]] = sorted(
        (max(0.0, d.t0), min(dur, d.t1)) for d in defects if d.t1 > d.t0)
    merged: list[list[float]] = []
    for t0, t1 in covered:
        if merged and t0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t1)
        else:
            merged.append([t0, t1])
    cov = sum(t1 - t0 for t0, t1 in merged)
    score = max(0.0, 1.0 - cov / dur)

    return QAVerdict("revise" if defects else "accept", score, defects, dur)


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("用法: python render_qa.py <out.mp4> [expected_duration_s]")
        raise SystemExit(1)
    exp = float(sys.argv[2]) if len(sys.argv) > 2 else None
    v = qa_render(Path(sys.argv[1]), expected_duration_s=exp,
                  evidence_dir=Path("qa_evidence"))
    print(json.dumps(v.to_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if v.verdict == "accept" else 1)