#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8 · SessionController —— 会话层状态机(设计文档 §2.1 会话层 / §11)。

一个 job 从第一条消息活到定稿。职责:
  - 收**结构化事件**(上游网关已区分上传/文本;文本只做轻量意图分类),
    决定本条消息触发什么:首次现写 / 增量重编 / 只入池不合成 / 定稿 / 忽略;
  - 管素材池(M5 AssetPool 注入):上传→入池;生成请求→登记 pending + 发起异步生成;
    生成完成/失败事件→更新池,完成且已有预览 → 增量重编(§11.3:不空等、不阻断);
  - **同 job 串行**(§9):per-session 锁,compose 在途时新事件排队顺序处理,
    绝不并发合成同一个 job;
  - 会话持久化:指令/轮次/定稿态存 JSON,跨进程重启存活(池由 M5 自持久化)。

合成能力注入(与 M7 的接线在 M9 统一做):
    compose_first_fn(ctx_dict)                    -> ComposeReport 形
    recompose_fn(prev_tsx, ctx_dict, change_note) -> ComposeReport 形
    generate_broll_fn(prompt, label) -> None      # 发起异步生成(结果走事件回来)

事件契约(handle_event 入参):
    {"type": "main_video", "path"}                  主视频到达
    {"type": "broll_upload", "path", "label"}       上传 b-roll
    {"type": "broll_generate", "prompt", "label"}   请求生成 b-roll
    {"type": "text", "text"}                        文本(意图分类:批准/修改指令)
    {"type": "generation_done", "asset_id", "path"} 生成完成(异步回调)
    {"type": "generation_failed", "asset_id", "reason"}

返回 SessionOutcome{action, report, user_message}——user_message 是给 WhatsApp
用户的中文答复;不抛业务异常。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 文本意图:批准词表(轻量规则,产品期可换成 planner 分类——单独可替换)
_APPROVE_WORDS = ("确认", "可以", "定稿", "没问题", "就这样", "通过", "ok", "OK", "好的")


@dataclass
class SessionOutcome:
    action: str                    # composed | recomposed | pooled | generating |
    #                                finalized | noted | ignored | error
    report: object | None = None   # ComposeReport 形(composed/recomposed 时有)
    user_message: str = ""


class SessionController:
    def __init__(self, job_dir: str | Path, pool,
                 compose_first_fn: Callable, recompose_fn: Callable,
                 generate_broll_fn: Callable | None = None,
                 extra_ctx: dict | None = None) -> None:
        self.job_dir = Path(job_dir)
        self.pool = pool
        self._compose_first = compose_first_fn
        self._recompose = recompose_fn
        self._generate = generate_broll_fn
        self._extra_ctx = extra_ctx or {}
        self._lock = threading.Lock()          # 同 job 串行(§9)
        # 会话状态
        self.main_video: str | None = None
        self.instruction: str = ""
        self.finalized: bool = False
        self.compose_count: int = 0
        self.last_tsx: str = ""
        self.last_report = None

    # ── 状态持久化(池由 M5 自己存)──
    def save(self) -> None:
        p = self.job_dir / "authored" / "session.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "main_video": self.main_video, "instruction": self.instruction,
            "finalized": self.finalized, "compose_count": self.compose_count,
            "last_tsx": self.last_tsx,
        }, ensure_ascii=False), encoding="utf-8")

    def restore(self) -> bool:
        p = self.job_dir / "authored" / "session.json"
        if not p.exists():
            return False
        d = json.loads(p.read_text(encoding="utf-8"))
        self.main_video = d.get("main_video")
        self.instruction = d.get("instruction", "")
        self.finalized = bool(d.get("finalized"))
        self.compose_count = int(d.get("compose_count", 0))
        self.last_tsx = d.get("last_tsx", "")
        return True

    # ── 事件入口(串行)──
    def handle_event(self, evt: dict) -> SessionOutcome:
        with self._lock:                       # 排队即串行:后到事件阻塞至前一个处理完
            try:
                out = self._dispatch(evt or {})
            except Exception as e:  # noqa: BLE001 —— 会话层不许炸
                out = SessionOutcome("error", None,
                                     f"处理消息时出了问题({type(e).__name__}),这条先跳过,其它不受影响。")
            self.save()
            return out

    # ── 分发 ──
    def _dispatch(self, evt: dict) -> SessionOutcome:
        etype = evt.get("type", "")
        if self.finalized and etype not in ("text",):
            return SessionOutcome("ignored", None,
                                  "这条视频已定稿。要继续改的话请发新视频开新任务。")

        if etype == "main_video":
            self.main_video = str(evt.get("path", ""))
            return self._compose(first=True, note="主视频到达,首次合成")

        if etype == "broll_upload":
            a = self.pool.add_upload(evt.get("path", ""), label=evt.get("label", ""))
            if a.status == "failed":
                return SessionOutcome("pooled", None,
                                      f"这段素材没法用({a.error}),请重传。其余不受影响。")
            if self.last_report is None:
                return SessionOutcome("pooled", None,
                                      f"收到 b-roll「{a.label or '未命名'}」。{self.pool.summary()}")
            return self._compose(first=False,
                                 note=f"新 b-roll「{a.label or a.id}」已就绪,合理编排进当前视频,其余保持不变。")

        if etype == "broll_generate":
            prompt = str(evt.get("prompt", ""))
            a = self.pool.add_generation(prompt, label=evt.get("label", ""))
            if self._generate is not None:
                self._generate(prompt, a.label)      # 异步发起;结果走 generation_done 事件
            return SessionOutcome("generating", None,
                                  f"开始生成 b-roll「{a.label}」,不影响当前出片;好了会自动补进视频。")

        if etype == "generation_done":
            a = self.pool.mark_generated(evt.get("asset_id", ""), evt.get("path", ""))
            if a is None or a.status != "ready":
                reason = a.error if a else "找不到对应的生成任务"
                return SessionOutcome("noted", None, f"生成的素材不可用({reason})。")
            if self.last_report is not None:
                return self._compose(first=False,
                                     note=f"生成的 b-roll「{a.label}」已就绪,合理编排进当前视频,其余保持不变。")
            if self.main_video:
                return self._compose(first=True, note="生成素材就绪且有主视频,首次合成")
            return SessionOutcome("pooled", None, f"b-roll「{a.label}」生成好了,等主视频到了一起合成。")

        if etype == "generation_failed":
            self.pool.mark_failed(evt.get("asset_id", ""), str(evt.get("reason", "生成失败")))
            return SessionOutcome("noted", None,
                                  f"有一段 b-roll 生成失败({evt.get('reason', '')}),"
                                  f"当前视频不受影响;要重试的话说一声。")

        if etype == "text":
            text = str(evt.get("text", "")).strip()
            if any(w in text for w in _APPROVE_WORDS) and len(text) <= 12:
                self.finalized = True
                return SessionOutcome("finalized", None, "已定稿。")
            if self.last_report is None:
                self.instruction = (self.instruction + "\n" + text).strip()
                return SessionOutcome("noted", None, "记下了,等视频到了按这个来。")
            return self._compose(first=False, note=f"用户修改要求:{text}")

        return SessionOutcome("ignored", None, "没识别出这条消息要做什么。")

    # ── 合成 ──
    def _build_ctx(self) -> dict:
        return {"main_video": self.main_video,
                "instruction": self.instruction,
                "broll": self.pool.resolve(),
                "pool_summary": self.pool.summary(),
                **self._extra_ctx}

    def _compose(self, first: bool, note: str) -> SessionOutcome:
        if not self.main_video:
            return SessionOutcome("noted", None, "还没收到主视频,先发视频吧。")
        ctx = self._build_ctx()
        if first or not self.last_tsx:
            rep = self._compose_first(ctx)
            action = "composed"
        else:
            rep = self._recompose(self.last_tsx, ctx, note)
            action = "recomposed"
        self.compose_count += 1
        self.last_report = rep
        status = getattr(rep, "status", "failed")
        if status in ("accepted", "accepted_best", "fallback"):
            self.last_tsx = getattr(rep, "tsx", "") or self.last_tsx
            tail = {"accepted": "", "accepted_best": "(本版还有小瑕疵,已取当前最好的一版)",
                    "fallback": "(本次走了稳定模板)"}[status]
            return SessionOutcome(action, rep, f"新预览出来了{tail}。{self.pool.summary()}")
        return SessionOutcome("error", rep,
                              "这次合成没成功,稍后我再试;上一版预览仍然有效。")