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
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

FPS = 30
WIDTH, HEIGHT = 1080, 1920

# 生成文件的模板。props 全量落成 props.json 由 Root 导入——不在 TSX 里内插用户数据,
# 免得转写文本里的引号/反斜杠把生成代码搞坏(注入面收敛为 JSON 序列化一处)。
_ROOT_TSX = """// 由 AuthoredRenderer 生成(job {token}),渲后即删。
import React from "react";
import {{ Composition, staticFile }} from "remotion";
import AuthoredScene from "./AuthoredScene";
import props from "./props.json";

export const Root: React.FC = () => (
  <Composition
    id="AuthoredScene"
    component={{AuthoredScene as any}}
    durationInFrames={{props.durationInFrames}}
    fps={{props.fps}}
    width={{props.width}}
    height={{props.height}}
    defaultProps={{{{
      ...props,
      videoSrc: staticFile(props.videoSrc),
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


def _stage_workspace(rc_dir: Path, token: str, tsx: str,
                     person_video: Path, broll: list[dict],
                     words: list[dict], duration_frames: int) -> tuple[Path, Path, Path]:
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

    props = {
        "videoSrc": main_rel, "broll": broll_props, "words": words,
        "fps": FPS, "durationInFrames": duration_frames,
        "width": WIDTH, "height": HEIGHT,
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
                    keep_workspace: bool = False) -> RenderResult:
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
            max(1, round(duration_s * FPS)))

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