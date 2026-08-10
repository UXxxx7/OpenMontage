#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 · ComposeOrchestrator —— 合成层状态机(设计文档 §2.1/§3)。

一次"现写 → 定稿"闭环的总控:
    author → validate → render → qa → (revise → validate → render → qa)* → 定稿
管三件事:轮数上限、墙钟/成本预算、失败落兜底。**任何情况下返回报告,不抛异常**
——SessionController 靠返回值驱动。

依赖全部注入(author_fn/validate_fn/render_fn/qa_fn/revise_fn/storyboard_fn/
fallback_fn),形态即 M1–M6 的产出契约;与真实模块的接线在 M9 集成时统一做,
本模块不 import 任何 m1–m6 代码——保证状态机可独立单测、可独立替换。

定稿三态(§3/§9):
    accepted       —— QA accept,正常出片
    accepted_best  —— 预算/轮数耗尽但手上有渲成的候选,择 QA score 最高者出片
    fallback       —— 连一条渲成的都没有 → 调 Arm A 兜底
    failed         —— 兜底也失败(现有产品既有失败面,worker 原有错误处理接手)

成本口径:每次 author/revise 的 usage 按 (price_in, price_out) 计价累加;
调用**前**检查 cost_ceiling 与墙钟,超顶立即停(不再发起新 LLM 调用)。
时钟可注入(clock)——测墙钟不用真等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
import time


@dataclass
class ComposeBudget:
    max_revise_rounds: int = 2
    wallclock_budget_s: float = 1800.0
    cost_ceiling_usd: float = 0.50
    price_in: float = 0.3          # $/M tokens
    price_out: float = 2.5


@dataclass
class ComposeReport:
    status: str                    # accepted | accepted_best | fallback | failed
    mp4: str | None = None
    tsx: str = ""
    rounds: int = 0                # 实际发生的 revise 轮数
    cost_usd: float = 0.0
    qa: dict | None = None
    storyboard: dict | None = None
    history: list = field(default_factory=list)   # 每步一条 {step, round, note}
    fell_back: bool = False
    error: str = ""


def _cost(usage: dict, b: ComposeBudget) -> float:
    billable_out = max(0, (usage or {}).get("total", 0) - (usage or {}).get("prompt", 0))
    return (usage or {}).get("prompt", 0) / 1e6 * b.price_in + billable_out / 1e6 * b.price_out


def _violations_to_defects(violations: list) -> list:
    """M1 违规 → 修订缺陷形态(与 M2 defects 同构,kind=validator)。"""
    return [{"t0": 0.0, "t1": 0.0, "kind": "validator",
             "desc": f"[{getattr(v, 'rule', v.get('rule', '?') if isinstance(v, dict) else '?')}] "
                     f"{getattr(v, 'detail', v.get('detail', '') if isinstance(v, dict) else '')}",
             "frame_path": None} for v in violations or []]


def _render_error_defect(render_result) -> list:
    tail = getattr(render_result, "log_tail", "") or ""
    return [{"t0": 0.0, "t1": 0.0, "kind": "render_error",
             "desc": "渲染失败,报错尾部:\n" + tail[-1200:], "frame_path": None}]


def compose(ctx,
            author_fn: Callable,        # (ctx) -> AuthorResult{ok,tsx,usage,error}
            validate_fn: Callable,      # (tsx) -> {ok, violations}
            render_fn: Callable,        # (tsx) -> {ok, out_path, log_tail}
            qa_fn: Callable,            # (mp4_path) -> {verdict, score, defects, to_dict()}
            revise_fn: Callable,        # (tsx, defects, ctx, notes) -> AuthorResult
            storyboard_fn: Callable | None = None,   # (tsx) -> dict
            fallback_fn: Callable | None = None,     # (ctx) -> mp4_path (Arm A 全中段)
            budget: ComposeBudget | None = None,
            clock: Callable[[], float] = time.monotonic) -> ComposeReport:
    b = budget or ComposeBudget()
    rep = ComposeReport(status="failed")
    t0 = clock()
    best: tuple[float, str, str, dict] | None = None   # (score, mp4, tsx, qa_dict)

    def log(step: str, rnd: int, note: str = ""):
        rep.history.append({"step": step, "round": rnd, "note": note[:300]})

    def over_budget() -> str:
        if clock() - t0 > b.wallclock_budget_s:
            return f"墙钟超预算({b.wallclock_budget_s}s)"
        if rep.cost_usd >= b.cost_ceiling_usd:
            return f"成本超顶(${b.cost_ceiling_usd})"
        return ""

    def settle() -> ComposeReport:
        """预算/轮数耗尽时的收口:有候选取最好,否则兜底。"""
        nonlocal best
        if best is not None:
            score, mp4, tsx, qa = best
            rep.status, rep.mp4, rep.tsx, rep.qa = "accepted_best", mp4, tsx, qa
            log("settle", rep.rounds, f"接受当前最好版 score={score:.3f}")
            _emit_storyboard(tsx)
            return rep
        return do_fallback("没有任何渲成的候选")

    def do_fallback(reason: str) -> ComposeReport:
        rep.fell_back = True
        log("fallback", rep.rounds, reason)
        if fallback_fn is None:
            rep.status, rep.error = "failed", f"需兜底但未接 fallback_fn:{reason}"
            return rep
        try:
            mp4 = fallback_fn(ctx)
            rep.status, rep.mp4 = "fallback", str(mp4)
        except Exception as e:  # noqa: BLE001 —— 兜底也炸=既有失败面,如实上报
            rep.status, rep.error = "failed", f"兜底失败: {type(e).__name__}: {e}"
        return rep

    def _emit_storyboard(tsx: str):
        if storyboard_fn is None:
            return
        try:
            rep.storyboard = storyboard_fn(tsx)
        except Exception as e:  # noqa: BLE001 —— 分镜失败不拖垮出片(§2.11 降级)
            log("storyboard", rep.rounds, f"分镜生成失败,跳过: {type(e).__name__}")

    try:
        # ── 首次现写 ──
        r = author_fn(ctx)
        rep.cost_usd += _cost(getattr(r, "usage", None) or {}, b)
        log("author", 0, "" if r.ok else r.error)
        if not r.ok:
            return do_fallback(f"author 失败: {r.error}")
        tsx = r.tsx

        # ── 闭环 ──
        while True:
            reason = over_budget()
            if reason:
                log("budget", rep.rounds, reason)
                return settle()

            v = validate_fn(tsx)
            if getattr(v, "ok", False):
                log("validate", rep.rounds, "通过")
            else:
                viols = getattr(v, "violations", [])
                log("validate", rep.rounds, f"{len(viols)} 处违规")
                if rep.rounds >= b.max_revise_rounds:
                    return settle()
                rr = revise_fn(tsx, _violations_to_defects(viols), ctx,
                               "代码未过安全/契约校验,按违规逐条修正,别的不动。")
                rep.cost_usd += _cost(getattr(rr, "usage", None) or {}, b)
                rep.rounds += 1
                log("revise", rep.rounds, "按校验违规重写")
                if not rr.ok:
                    return settle() if best else do_fallback(f"revise 失败: {rr.error}")
                tsx = rr.tsx
                continue

            rend = render_fn(tsx)
            if getattr(rend, "ok", False):
                log("render", rep.rounds, "成功")
            else:
                log("render", rep.rounds, "渲染失败")
                if rep.rounds >= b.max_revise_rounds or over_budget():
                    return settle()
                rr = revise_fn(tsx, _render_error_defect(rend), ctx,
                               "渲染失败,按报错修正代码,别的不动。")
                rep.cost_usd += _cost(getattr(rr, "usage", None) or {}, b)
                rep.rounds += 1
                log("revise", rep.rounds, "按渲染报错重写")
                if not rr.ok:
                    return settle() if best else do_fallback(f"revise 失败: {rr.error}")
                tsx = rr.tsx
                continue

            qa = qa_fn(rend.out_path)
            qa_dict = qa.to_dict() if hasattr(qa, "to_dict") else dict(qa)
            score = float(getattr(qa, "score", qa_dict.get("score", 0)))
            log("qa", rep.rounds, f"{qa_dict.get('verdict')} score={score:.3f}")
            if best is None or score > best[0]:
                best = (score, rend.out_path, tsx, qa_dict)

            if qa_dict.get("verdict") == "accept":
                rep.status, rep.mp4, rep.tsx, rep.qa = "accepted", rend.out_path, tsx, qa_dict
                _emit_storyboard(tsx)
                return rep

            if rep.rounds >= b.max_revise_rounds or over_budget():
                return settle()
            defects = qa_dict.get("defects", [])
            rr = revise_fn(tsx, defects, ctx, "")
            rep.cost_usd += _cost(getattr(rr, "usage", None) or {}, b)
            rep.rounds += 1
            log("revise", rep.rounds, f"按 {len(defects)} 处 QA 缺陷重写")
            if not rr.ok:
                return settle()
            tsx = rr.tsx

    except Exception as e:  # noqa: BLE001 —— 状态机自身不许炸(§9 最后一行)
        rep.error = f"orchestrator 异常: {type(e).__name__}: {e}"
        return do_fallback(rep.error)