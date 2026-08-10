#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5 · AssetResolver / AssetPool —— 多来源 b-roll 素材池(设计文档 §11.2)。

职责:把"用户上传的"与"AI 生成的" b-roll 统一成一份**已解析**的素材列表,
供 SceneAuthor 消费。SceneAuthor 永远只面对"一份干净的 broll[] prop",
素材从哪来、几段、第几轮到、生成成没成功——这些复杂度全部在这里吸收。

资产模型(§11.2 契约):
    {id, src, label, source: uploaded|generated, status: ready|pending|failed,
     duration_s, suggested_window: [t0,t1]|None, error}

关键行为:
  - add_upload():文件必须存在、非空、ffprobe 能读出时长,才 ready;否则 failed(带原因)。
    同内容重复上传(按 sha256 前 1MB+大小)去重——返回已有资产,不产生重复条目。
  - add_generation():立刻登记为 pending(生成是异步的,**不阻塞出预览**);
    生成完成 mark_generated(id, path) → 走与上传相同的探测转 ready;
    生成失败 mark_failed(id, reason) → failed,据实告知用户,不阻断出片。
  - resolve(fps):只输出 ready 资产,按加入顺序编号,产出渲染用 broll[] prop
    (suggested_window 秒 → startFrame/endFrame 帧;没建议窗口则 0/0,交给模型定)。
  - summary():给用户/日志的一句话现状(几段就绪/生成中/失败)。
  - to_json()/from_json():落盘持久化(job_dir/authored/assets.json),跨消息轮次存活。

不做的事:不猜"该放在视频哪里"(那是 SceneAuthor/上游匹配的事);
不重试生成(重试策略归 SessionController);不删文件。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

READY, PENDING, FAILED = "ready", "pending", "failed"
UPLOADED, GENERATED = "uploaded", "generated"


def _probe_duration(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return float(r.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return 0.0


def _content_key(p: Path) -> str:
    """去重键:大小 + 前 1MB sha256(整文件哈希对大视频太慢,前段+大小足够辨重复上传)。"""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(1024 * 1024))
    return f"{p.stat().st_size}:{h.hexdigest()[:16]}"


@dataclass
class Asset:
    id: str
    src: str | None            # 落盘路径;pending 时为 None
    label: str
    source: str                # uploaded | generated
    status: str                # ready | pending | failed
    duration_s: float = 0.0
    suggested_window: list | None = None   # [t0, t1] 秒;None=交给模型定
    error: str = ""
    gen_prompt: str = ""       # generated 专用:生成提示(便于重试/审计)
    content_key: str = ""      # 去重键(ready 后填)


class AssetPool:
    def __init__(self) -> None:
        self._assets: list[Asset] = []

    # ── 上传件 ──
    def add_upload(self, path: str | Path, label: str = "",
                   suggested_window: list | None = None) -> Asset:
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            a = Asset(id=f"a_{uuid.uuid4().hex[:8]}", src=str(p), label=label,
                      source=UPLOADED, status=FAILED,
                      error="文件不存在或为空")
            self._assets.append(a)
            return a
        key = _content_key(p)
        for existing in self._assets:                      # 内容级去重
            if existing.content_key == key and existing.status == READY:
                if label and not existing.label:
                    existing.label = label                 # 后补的说明别浪费
                return existing
        dur = _probe_duration(p)
        if dur <= 0:
            a = Asset(id=f"a_{uuid.uuid4().hex[:8]}", src=str(p), label=label,
                      source=UPLOADED, status=FAILED,
                      error="ffprobe 读不出时长(不是可用的视频文件)")
        else:
            a = Asset(id=f"a_{uuid.uuid4().hex[:8]}", src=str(p), label=label,
                      source=UPLOADED, status=READY, duration_s=round(dur, 2),
                      suggested_window=suggested_window, content_key=key)
        self._assets.append(a)
        return a

    # ── 生成件(异步)──
    def add_generation(self, gen_prompt: str, label: str = "") -> Asset:
        a = Asset(id=f"a_{uuid.uuid4().hex[:8]}", src=None,
                  label=label or gen_prompt[:24], source=GENERATED,
                  status=PENDING, gen_prompt=gen_prompt)
        self._assets.append(a)
        return a

    def mark_generated(self, asset_id: str, path: str | Path) -> Asset | None:
        a = self.get(asset_id)
        if a is None or a.status != PENDING:
            return None
        p = Path(path)
        dur = _probe_duration(p) if p.exists() and p.stat().st_size > 0 else 0.0
        if dur <= 0:
            a.status, a.error = FAILED, "生成产物缺失或不可读"
        else:
            a.status, a.src, a.duration_s = READY, str(p), round(dur, 2)
            a.content_key = _content_key(p)
        return a

    def mark_failed(self, asset_id: str, reason: str) -> Asset | None:
        a = self.get(asset_id)
        if a is None or a.status != PENDING:
            return None
        a.status, a.error = FAILED, reason
        return a

    # ── 查询 / 输出 ──
    def get(self, asset_id: str) -> Asset | None:
        return next((a for a in self._assets if a.id == asset_id), None)

    def all(self) -> list:
        return list(self._assets)

    def resolve(self, fps: int = 30) -> list:
        """ready 资产 → 渲染/authoring 用的 broll[] prop(§4 契约)。"""
        out = []
        for a in self._assets:
            if a.status != READY:
                continue
            if a.suggested_window and len(a.suggested_window) == 2:
                sf = max(0, round(float(a.suggested_window[0]) * fps))
                ef = max(sf, round(float(a.suggested_window[1]) * fps))
            else:
                sf = ef = 0                                # 0/0 = 交给模型定
            out.append({"src": a.src, "label": a.label,
                        "startFrame": sf, "endFrame": ef})
        return out

    def summary(self) -> str:
        n = {READY: 0, PENDING: 0, FAILED: 0}
        for a in self._assets:
            n[a.status] += 1
        parts = [f"{n[READY]} 段就绪"]
        if n[PENDING]:
            parts.append(f"{n[PENDING]} 段生成中(不阻塞出片)")
        if n[FAILED]:
            fails = [f"「{a.label}」({a.error})" for a in self._assets if a.status == FAILED]
            parts.append(f"{n[FAILED]} 段失败:" + "、".join(fails))
        return "b-roll:" + ",".join(parts)

    # ── 持久化 ──
    def to_json(self) -> str:
        return json.dumps([asdict(a) for a in self._assets], ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "AssetPool":
        pool = cls()
        for d in json.loads(text):
            pool._assets.append(Asset(**d))
        return pool

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "AssetPool":
        p = Path(path)
        return cls.from_json(p.read_text(encoding="utf-8")) if p.exists() else cls()