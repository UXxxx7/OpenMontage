#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3 · AuthoredRenderer —— per-job 隔离渲染(设计文档 §2.4)。

把过了 M1 安全闸的 AuthoredScene.tsx 渲成 mp4。三条硬要求:

1) **并发安全(per-job bundle)**:每次渲染在 remotion-composer 里创建
   `src/.armb/<job_token>/`(生成代码)与 `public/.armb/<job_token>/`(素材副本)
   两个**独占**目录,入口/素材路径全部带 job_token——两个 job 同时渲互不相扰。
   绝不写共享可变文件(设计评审否掉的"常驻槽"方案)。
2) **隔离 + 硬超时**:渲染在独立进程组里跑(start_new_session),超时对整个
   进程组 SIGKILL(npx 会再起 node 子进程,单杀父进程杀不干净)。
3) **必清理**:成功/失败/超时/异常,一律在 finally 里删掉本次的 .armb 目录
   (keep_workspace=True 供排查时保留)。

渲染命令可注入(render_cmd):默认 `npx remotion render <entry> AuthoredScene <out>`;
测试时注入假渲染器即可全量验证编排逻辑,真渲在有 node_modules 的机器上验收。

返回 RenderResult{ok, out_path, exit_code, timed_out, log_tail, duration_ms}——
不抛业务异常:Orchestrator 靠返回值驱动(修一轮或兜底)。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FPS = 30
WIDTH, HEIGHT = 1080, 1920

# 生成文件的模板。props 全量落成 props.json 由 Root 导入——不在 TSX 里内插用户数据,
# 免得转写文本里的引号/反斜杠把生成代码搞坏(注入面收敛为 JSON 序列化一处)。
#
# Mid-video cuts (Phase 8+, "Arm B razor cuts") — the composition no longer
# mounts AuthoredScene directly. AuthoredCutWrapper (a hand-written,
# non-AI-authored component at src/components/xiaojin/AuthoredCutWrapper.tsx,
# shared byte-for-byte with the live browser preview via
# editor/components/Authored/AuthoredPreview.tsx — see that file's own
# comment) sits in between: it renders the (possibly cut) base video via the
# already-generic, already-production-proven CutVideo, computes a
# `sourceFrame` value (SOURCE-frame space, via src/cuts.ts's
# outputToSource — the SAME generic frame-remap functions Arm A's own cuts
# feature uses, imported unmodified), and mounts AuthoredScene with that
# value injected as a prop. This is the interception point because Arm B
# has no structured props tree to remap the way Arm A's mapPropsForCuts
# does — every AI-authored scene's own timing logic is literal numbers
# baked into generated code, driven by exactly one `frame` value (confirmed:
# every real generated scene declares `const frame = useCurrentFrame();`
# once near the top) — so remapping what `frame` itself resolves to is the
# only generically-correct lever, and it works for EVERY job without
# parsing or trusting that job's own code.
#
# `videoSrc` is intentionally NOT staticFile()'d here (unlike before this
# feature) — CutVideo already resolves it internally
# (`src.startsWith("http") ? src : staticFile(src)`), matching exactly how
# Arm A's own XiaojinEditorial composition passes a raw relative path all
# the way down to the same CutVideo component. Resolving it twice risked
# double-wrapping; passing it raw here is the one convention both arms share.
_ROOT_TSX = """// 由 AuthoredRenderer 生成(job {token}),渲后即删。
import React from "react";
import {{ Composition, CalculateMetadataFunction, staticFile }} from "remotion";
import AuthoredScene from "./AuthoredScene";
import {{ AuthoredCutWrapper }} from "../../components/xiaojin/AuthoredCutWrapper";
import {{ normalizeCuts, totalCutFrames }} from "../../cuts";
import props from "./props.json";

type AuthoredRootProps = typeof props;

const calculateMetadata: CalculateMetadataFunction<AuthoredRootProps> = async ({{ props: p }}) => {{
  const srcLen = Math.max(1, p.durationInFrames);
  const cuts = normalizeCuts((p as any).videoCuts, srcLen);
  const durationInFrames = Math.max(1, srcLen - totalCutFrames(cuts));
  return {{ durationInFrames, fps: p.fps, width: p.width, height: p.height }};
}};

const AuthoredCutRoot: React.FC<AuthoredRootProps> = (p) => (
  <AuthoredCutWrapper
    videoSrc={{p.videoSrc}}
    videoVolume={{(p as any).videoVolume}}
    cuts={{(p as any).videoCuts}}
    sourceDurationFrames={{p.durationInFrames}}
    component={{AuthoredScene as any}}
    sceneProps={{p}}
  />
);

export const Root: React.FC = () => (
  <Composition
    id="AuthoredScene"
    component={{AuthoredCutRoot}}
    calculateMetadata={{calculateMetadata}}
    fps={{props.fps}}
    width={{props.width}}
    height={{props.height}}
    defaultProps={{{{
      ...props,
      broll: props.broll.map((b: any) => ({{ ...b, src: staticFile(b.src) }})),
    }}}}
  />
);
"""

_INDEX_TS = """// 由 AuthoredRenderer 生成(job {token}),渲后即删。
import {{ registerRoot }} from "remotion";
import {{ Root }} from "./Root";
registerRoot(Root);
"""


@dataclass
class RenderResult:
    ok: bool
    out_path: str | None
    exit_code: int | None
    timed_out: bool
    log_tail: str
    duration_ms: int


def _words_base_stamp(words: list[dict]) -> str:
    """Mirrors editor/state/authoredCaptions.ts's wordsBaseStamp exactly —
    a cheap fingerprint (length + first/last timing), not a full-content
    hash, of the words array a `__captions` override was computed against.
    Python's str(float) and JS's default Number->string both use the
    shortest round-tripping representation, so the same IEEE754 double
    (round-tripped through the same JSON payload both languages read)
    stringifies identically in both — safe to compare across the client/
    server boundary without agreeing on an explicit float format."""
    if not words:
        return "0"
    return f"{len(words)}:{words[0]['start']}:{words[-1]['end']}"


def _apply_caption_override(words: list[dict], captions_override) -> list[dict]:
    """Validates and applies the `__captions` reserved overrides key (Phase
    8 captions editing) — popped separately from `__scene` in
    _stage_workspace below, since `__scene` is spread FIRST onto props
    specifically so it can never clobber a computed value, but `words` IS
    the computed value here; `__captions` needs its own real validation,
    not that guard.

    Never raises and never blocks the render — any shape problem or a
    staleness mismatch (the job's transcript was regenerated since this
    edit was captured, detected via _words_base_stamp) degrades silently to
    the original `words`, same philosophy as scene_author.py's own
    _parse_and_validate_manifest degrading to `[]` rather than failing the
    whole result."""
    if not isinstance(captions_override, dict):
        return words
    new_words = captions_override.get("words")
    if not isinstance(new_words, list) or not new_words:
        logger.warning("__captions override missing/empty words list — degrading to authored transcript")
        return words

    base = captions_override.get("base")
    if base is not None and base != _words_base_stamp(words):
        logger.warning("__captions override's base stamp doesn't match the current transcript "
                        "(likely regenerated since the edit was made) — degrading to authored transcript")
        return words

    validated: list[dict] = []
    prev_end = float("-inf")
    for w in new_words:
        if not isinstance(w, dict):
            logger.warning("__captions override contains a non-object word entry — degrading to authored transcript")
            return words
        word_text, start, end = w.get("word"), w.get("start"), w.get("end")
        if not isinstance(word_text, str):
            logger.warning("__captions override word entry missing string 'word' — degrading to authored transcript")
            return words
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            logger.warning("__captions override word entry has non-numeric start/end — degrading to authored transcript")
            return words
        start, end = float(start), float(end)
        if not (start <= end):
            logger.warning("__captions override word entry has end < start — degrading to authored transcript")
            return words
        if start < prev_end - 1e-6:
            logger.warning("__captions override words aren't in non-decreasing time order — degrading to authored transcript")
            return words
        prev_end = end
        validated.append({"word": word_text, "start": start, "end": end})

    if len(validated) > 5000:  # generous — real jobs run 100-200 words; guards against a malicious/corrupt huge payload
        logger.warning("__captions override has an implausible word count (%d) — degrading to authored transcript", len(validated))
        return words

    return validated


def _apply_cuts_override(cuts_override, duration_frames: int) -> list[dict]:
    """Validates the `__cuts` reserved overrides key (mid-video razor cuts)
    — popped separately from `__scene`/`__captions` in _stage_workspace
    below (same reasoning: this becomes a NEW top-level `props.videoCuts`
    field, not something AuthoredScene's own `props.overrides` should ever
    see — only AuthoredCutWrapper reads it).

    Shape is the SAME `VideoCut[]` ({fromFrame, toFrame}[], SOURCE-frame
    half-open removed ranges) Arm A's src/cuts.ts already defines — actual
    clamping/merge/overlap normalization happens client-side AND again at
    render time inside the generated Root.tsx's calculateMetadata (both via
    normalizeCuts, the single source of truth), so this validation only
    needs to reject structurally invalid input, not fully replicate that
    math in Python. Never raises and never blocks the render — degrades to
    `[]` (no cuts) on any shape problem, same philosophy as
    _apply_caption_override and scene_author.py's own manifest degradation."""
    if not isinstance(cuts_override, list):
        return []
    validated: list[dict] = []
    for c in cuts_override:
        if not isinstance(c, dict):
            logger.warning("__cuts override contains a non-object entry — dropping that entry")
            continue
        from_frame, to_frame = c.get("fromFrame"), c.get("toFrame")
        if not isinstance(from_frame, (int, float)) or not isinstance(to_frame, (int, float)):
            logger.warning("__cuts override entry has non-numeric fromFrame/toFrame — dropping that entry")
            continue
        from_frame, to_frame = int(from_frame), int(to_frame)
        if not (0 <= from_frame < to_frame <= max(1, duration_frames)):
            logger.warning("__cuts override entry out of range or empty (fromFrame=%r toFrame=%r, "
                            "duration=%r) — dropping that entry", from_frame, to_frame, duration_frames)
            continue
        validated.append({"fromFrame": from_frame, "toFrame": to_frame})

    if len(validated) > 500:  # generous — a real edit session makes a handful of cuts, not hundreds
        logger.warning("__cuts override has an implausible cut count (%d) — degrading to no cuts", len(validated))
        return []

    return validated


def _stage_workspace(rc_dir: Path, token: str, tsx: str,
                     person_video: Path, broll: list[dict],
                     words: list[dict], duration_frames: int,
                     overrides: dict | None = None) -> tuple[Path, Path, Path]:
    """落 per-job 生成代码 + 素材副本。返回 (src_ws, pub_ws, entry)。"""
    src_ws = rc_dir / "src" / ".armb" / token
    pub_ws = rc_dir / "public" / ".armb" / token
    src_ws.mkdir(parents=True, exist_ok=False)   # 独占:已存在=token 撞了,宁可炸
    pub_ws.mkdir(parents=True, exist_ok=False)

    # 素材副本(public 下,staticFile 相对路径带 token)
    main_rel = f".armb/{token}/main.mp4"
    shutil.copyfile(person_video, pub_ws / "main.mp4")
    broll_props = []
    for i, b in enumerate(broll):
        rel = f".armb/{token}/broll_{i}.mp4"
        shutil.copyfile(b["src"], pub_ws / f"broll_{i}.mp4")
        broll_props.append({
            "src": rel, "label": str(b.get("label", f"b{i}")),
            "startFrame": int(b.get("startFrame", 0)),
            "endFrame": int(b.get("endFrame", 0)),
        })

    # "__scene" is a reserved overrides key for SCENE-level settings (currently
    # just videoVolume) that aren't tied to any one manifest element — using a
    # dedicated key inside the existing `overrides` dict, instead of a new
    # top-level field, needed zero changes to the /overrides POST route or its
    # request-body shape. Popped back out here so props.overrides only ever
    # holds real per-element data (`props.overrides?.["<id>"]?.<field>` never
    # needs to know "__scene" is special) and merged onto top-level props
    # instead, where scene_author.py's prompt tells the model to read
    # `props.videoVolume` directly.
    overrides = dict(overrides or {})
    scene_overrides = overrides.pop("__scene", {}) or {}
    # "__captions" is a second reserved overrides key (Phase 8 captions
    # editing), popped SEPARATELY from "__scene" — it must not ride on that
    # key's spread-first guard, since `words` is itself one of the computed
    # values that guard protects; this one genuinely REPLACES `words`
    # instead, after real validation (see _apply_caption_override).
    captions_override = overrides.pop("__captions", None)
    words = _apply_caption_override(words, captions_override)
    # "__cuts" is a third reserved overrides key (mid-video razor cuts) —
    # becomes a NEW top-level `videoCuts` field the generated Root.tsx's
    # AuthoredCutWrapper reads; AuthoredScene's own `props.overrides` must
    # never see it (same reasoning as __scene/__captions above).
    cuts_override = overrides.pop("__cuts", None)
    video_cuts = _apply_cuts_override(cuts_override, duration_frames)

    props = {
        # scene_overrides spread FIRST — a stray key inside it (there
        # shouldn't be one; this dict only ever holds videoVolume today)
        # must never be able to clobber the real computed values below.
        **scene_overrides,
        "videoSrc": main_rel, "broll": broll_props, "words": words,
        "fps": FPS, "durationInFrames": duration_frames,
        "width": WIDTH, "height": HEIGHT,
        "videoCuts": video_cuts,
        # Phase 8 — the user's accumulated manual edits (editable-manifest
        # overrides). Flows through Root.tsx's `...props` spread unchanged
        # (that template maps broll specifically for staticFile() wrapping;
        # overrides needs no such rewriting).
        "overrides": overrides,
    }
    (src_ws / "AuthoredScene.tsx").write_text(tsx, encoding="utf-8")
    (src_ws / "props.json").write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    (src_ws / "Root.tsx").write_text(_ROOT_TSX.format(token=token), encoding="utf-8")
    entry = src_ws / "index.ts"
    entry.write_text(_INDEX_TS.format(token=token), encoding="utf-8")
    return src_ws, pub_ws, entry


def _default_render_cmd(entry: Path, out_path: Path) -> list:
    """entry 此处已是**相对 rc_dir** 的 POSIX 路径(render_authored 里算好),
    直接传给 CLI。曾用字符串切 '/remotion-composer/' 相对化——在 Windows +
    相对 rc_dir 下切不中,整条反斜杠路径被 Remotion 当成 composition ID
    回落到项目默认入口(真实验收抓到的 bug),故改为上游 relative_to 计算。"""
    npx = shutil.which("npx") or "npx"
    return [npx, "remotion", "render", entry.as_posix(), "AuthoredScene",
            str(out_path), "--crf=18"]


def render_authored(tsx: str,
                    person_video: Path,
                    broll: list[dict],
                    words: list[dict],
                    duration_s: float,
                    out_path: Path,
                    rc_dir: Path = Path("remotion-composer"),
                    timeout_s: int = 900,
                    render_cmd=None,          # 可注入:f(entry, out)->list[str];默认 npx remotion
                    keep_workspace: bool = False,
                    overrides: dict | None = None) -> RenderResult:  # Phase 8 — 手动编辑层
    t_start = time.monotonic()
    rc_dir = Path(rc_dir)
    out_path = Path(out_path).resolve()
    if not rc_dir.exists():
        return RenderResult(False, None, None, False,
                            f"remotion-composer 目录不存在:{rc_dir}", 0)
    if not Path(person_video).exists():
        return RenderResult(False, None, None, False,
                            f"主视频不存在:{person_video}", 0)

    token = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    src_ws = pub_ws = None
    log_tail, exit_code, timed_out = "", None, False
    try:
        src_ws, pub_ws, entry = _stage_workspace(
            rc_dir, token, tsx, Path(person_video), broll, words,
            max(1, round(duration_s * FPS)), overrides=overrides)

        # 入口一律用**相对 rc_dir** 的路径(cwd 即 rc_dir):跨平台稳定,
        # 也避免 Windows 反斜杠绝对路径被 CLI 误读
        entry_rel = entry.resolve().relative_to(rc_dir.resolve())
        cmd = (render_cmd or _default_render_cmd)(entry_rel, out_path)
        # 独立进程组:超时杀整组(npx→node 子进程树)
        proc = subprocess.Popen(
            cmd, cwd=str(rc_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            exit_code = proc.returncode
            log_tail = (out or "")[-2000:]
        except subprocess.TimeoutExpired:
            timed_out = True
            # os.killpg/os.getpgid 在 Windows 上根本不存在（AttributeError，
            # 不是 ProcessLookupError/PermissionError，原来的 except 分支接不
            # 住）——超时清理本身直接崩溃，真正的子进程树（npx→node→headless
            # Chrome）从未被杀，残留下来吃内存/CPU，越测越慢。Windows 上改用
            # `taskkill /T /F`（按 PID 杀整棵进程树，语义上等价于 killpg）。
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=15)
                except Exception:
                    proc.kill()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            proc.wait()
            log_tail = f"渲染超时(>{timeout_s}s),进程组已杀"

        ok = (exit_code == 0) and (not timed_out) and out_path.exists() \
            and out_path.stat().st_size > 0
        if not ok and out_path.exists() and out_path.stat().st_size == 0:
            log_tail += "\n(产物为 0 字节,按失败处理)"
        return RenderResult(ok, str(out_path) if ok else None, exit_code,
                            timed_out, log_tail,
                            int((time.monotonic() - t_start) * 1000))
    except Exception as e:  # noqa: BLE001 —— 编排层不抛,交给返回值
        return RenderResult(False, None, exit_code, timed_out,
                            f"{log_tail}\n编排异常: {type(e).__name__}: {e}",
                            int((time.monotonic() - t_start) * 1000))
    finally:
        if not keep_workspace:
            for ws in (src_ws, pub_ws):
                if ws is not None:
                    shutil.rmtree(ws, ignore_errors=True)
            # .armb 空壳目录也顺手清掉(留着无害,清了干净)
            for parent in (rc_dir / "src" / ".armb", rc_dir / "public" / ".armb"):
                try:
                    parent.rmdir()
                except OSError:
                    pass