#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whatsapp_mvp/authored/__init__.py —— M9 · Arm B 接线层(即设计文档的 authored_compose)。

把 M1–M7 绑成两个对外函数,供 pipeline_runner 的 6 行门调用:

    arm_b_enabled(job) -> bool      # 环境变量开关 + 按 job.id 哈希灰度
    compose_authored(job) -> dict|None
        # dict  = 与 Arm A 返回同构的结果(preview.mp4 已就位)→ 直接 return
        # None  = Arm B 没出片(任何原因)→ 调用方落穿进现有 Arm A 代码体(兜底)

设计决定(已拍板):
  - 兜底 = 落穿现有代码体,不实现独立 FallbackToArmA;本函数**永不抛异常**。
  - 本期不接 SessionController/多轮。
  - 分镜的模型叙述(画面设计描述):plan/revise 阶段**开启**(ARM_B_STORYBOARD_NARRATIVE
    默认 1,让颜色/特效类改动能在计划里体现;每次 plan/revise 多一次 describe 调用,
    设 0 可关);confirm 后 compose 内重出分镜**不再调**,纯 derived,避免重复计费。
  - 不改 config.py:开关/预算全走环境变量(与队友的 config 改动零冲突):
        ARM_B_ENABLED=1            总开关(默认 0=关)
        ARM_B_PERCENT=100          灰度百分比(按 job.id 稳定哈希;默认 100)
        ARM_B_MAX_ROUNDS=1         修订轮上限
        ARM_B_RENDER_TIMEOUT_S=600 渲染硬超时(实测 27.6s 素材约 48s 渲完)
        ARM_B_COST_CEILING=0.30    单 job LLM 现金顶($)
        ARM_B_STYLE_REFS=a.png;b.png  风格参考图(分号分隔,可空)
  - 转写复用现有 _safe_transcribe(函数内 lazy import,避免与 pipeline_runner 循环引用);
    b-roll 从 Arm A 计划的 insert_broll items 里收集**能落到本地文件**的项,
    解析不出的跳过(AssetPool 兜),一段都没有也照样合成(模型只做字幕+图形节拍)。
  - 渲染包在现有 RENDER_SLOTS 并发闸里,尊重单机重活排队约定。
  - 产物:job_dir/authored/{scene_r*.tsx, render_r*.mp4, compose_report.json,
    storyboard.json, storyboard.md, qa/};定稿片复制为 job_dir/preview.mp4。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

from .tsx_validator import validate_tsx
from .render_qa import qa_render
from .authored_renderer import render_authored
from .scene_author import AuthorContext, author_scene, revise_scene, describe_scene
from .storyboard_emitter import emit_storyboard, render_text
from .compose_orchestrator import compose, ComposeBudget

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def arm_b_enabled(job) -> bool:
    # 路由决策交给分层开关 arm_router(#armb/#arma 标签、自然语言、用户偏好文件、
    # 配置文件灰度、env 兜底)。委托**内嵌在此**(不再靠单独补丁),这样即使
    # 覆盖本文件也不会丢失路由逻辑——2026-07-27 就是因覆盖 __init__.py 把补丁冲掉
    # 导致全走 Arm A。签名不变(返回 bool),pipeline 的门无需改动。
    from .arm_router import resolve_arm
    return resolve_arm(job) == "arm_b"


# ─────────────────────────── 输入收集 ───────────────────────────

def _get_transcript(job, src: str) -> dict | None:
    """复用现有转写(带缓存);lazy import 破循环。失败返回 None(→落穿)。

    对锚仓库真实形状(离线验收①抓到的 bug):_safe_transcribe 返回 **ToolResult
    对象**(载荷在 .data,如 `t.data.get("word_timestamps")`),不可用时 None;
    兼容直接返回 dict 的旧/桩形状。"""
    try:
        from ..pipeline_runner import _safe_transcribe
        from ..config import get_config
        t = _safe_transcribe(src, job.job_dir, get_config().faster_whisper_model)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ArmB: 转写不可用,落穿 Arm A: {e}")
        return None
    if t is None:
        return None
    data = getattr(t, "data", None)          # ToolResult → .data;dict → 本体
    if not isinstance(data, dict):
        data = t if isinstance(t, dict) else None
    if not data or not data.get("word_timestamps"):
        return None
    return data


def _collect_broll(job) -> list:
    """从 Arm A 计划的 insert_broll items 收集能解析到本地文件的 b-roll。

    对锚自你仓库 pipeline_runner 的真实约定(不是猜的):
      - 计划操作的键是 **"type"**(如 {"type": "insert_broll", "items": [...]});
        兼容 "operation" 以防版本差异。
      - 上传素材落在 **job_dir/assets/broll_<asset_ref>.***(Phase 1 已下载),
        item 用整数 asset_ref 引用;直接给路径的字段(src/path/…)也兼容。
      - gen_prompt(要现生成的)本期跳过——生成归 Arm A 的 insert_broll 环节,
        Arm B 第一期只用已落盘素材。
    解析不出的跳过;一段都没有 → 返回 [](Arm B 照常合成,只做字幕+图形节拍)。"""
    out = []
    try:
        from ..pipeline_runner import _load_plan
        plan = _load_plan(job)
    except Exception:  # noqa: BLE001
        return out
    assets_dir = Path(job.job_dir) / "assets"
    for op in plan.get("edit_operations", []) or []:
        if (op.get("type") or op.get("operation")) != "insert_broll":
            continue
        for it in op.get("items", []) or []:
            cand = None
            ref = it.get("asset_ref")
            if ref is not None and assets_dir.exists():          # 主路径:asset_ref
                matches = sorted(assets_dir.glob(f"broll_{ref}.*"))
                if matches:
                    cand = matches[0]
            if cand is None:                                      # 兼容:直接路径字段
                for key in ("src", "path", "asset_path", "local_path", "file"):
                    v = it.get(key)
                    if v and Path(v).exists():
                        cand = Path(v); break
                    if v and (Path(job.job_dir) / str(v)).exists():
                        cand = Path(job.job_dir) / str(v); break
            if cand is None:
                continue                                          # gen_prompt 等本期跳过
            fps = 30
            sf = round(float(it.get("start_seconds") or 0) * fps)
            ef = round(float(it.get("end_seconds") or 0) * fps)
            out.append({"src": str(cand), "label": str(it.get("label") or cand.stem),
                        "startFrame": max(0, sf), "endFrame": max(0, ef)})
    if not out and assets_dir.exists():
        # 兜底(WhatsApp 实测发现):规划器可能拒绝/漏排 insert_broll,但素材在
        # Phase 1 就已下载到 assets/。计划里收不到时直接扫目录——Arm B 本来就
        # 不依赖规划器的方案,时间窗给 0/0 交给模型按转写内容自己定。
        for p in sorted(assets_dir.glob("broll_*.*")):
            _st = p.stem
            if _st.startswith("broll_gen") and _st[9:10].isdigit():
                continue   # Arm B 生成型(broll_gen<数字>)由 _gen_broll_entries 登记,勿重复计
            if p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                out.append({"src": str(p), "label": p.stem,
                            "startFrame": 0, "endFrame": 0})
    return out


def _style_refs() -> list:
    raw = os.getenv("ARM_B_STYLE_REFS", "")
    return [s for s in (x.strip() for x in raw.split(";")) if s and Path(s).exists()]


def _resolve_reference(job_dir: Path):
    """找 job 根目录下的参考素材 style_ref.*(模块4 由 /jobs 落盘)。返回 Path 或 None。"""
    cands = sorted(p for p in Path(job_dir).glob("style_ref.*") if p.is_file())
    return cands[0] if cands else None


def _record_style_cost(job_dir, cost_usd, source: str = "style_reference") -> None:
    """把参考分析花费按 pipeline 账本格式记进 job_dir/_generation_costs.json。
    _read_generation_cost 汇总 → job.generation_cost_usd(预览"💰"行)。失败静默,不拖垮现写。
    source 区分整体(style_reference)与聚焦(style_reference_focus)两层花费。"""
    if not cost_usd:
        return
    try:
        from ..pipeline_runner import _record_generation_cost   # lazy import 破循环
        _record_generation_cost(Path(job_dir), str(source), float(cost_usd))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ArmB style: 参考分析记账失败(忽略): {type(e).__name__}: {e}")


def _author_frames(job_dir, out_dir, spec) -> list:
    """给现写模型看的参考帧——分析抽多少就【全给】(帧数已按参考时长自适应)。
    分析产出的帧优先;native/无帧时为现写单独抽(纯 ffmpeg,自适应帧数)。
    决定"像不像"的关键——文字风格谱丢掉视觉感受(尤其配色),让模型直接看参考画面最有效。"""
    src = [f for f in ((spec or {}).get("source_frames") or []) if f and Path(f).exists()]
    if src:
        return src
    ref = _resolve_reference(job_dir)
    if ref is None:
        return []
    adir = Path(out_dir) / ".style_ref_author"
    have = sorted(adir.glob("frame_*.jpg")) if adir.exists() else []
    if have:
        return [str(p) for p in have]
    try:
        try:
            from .style_reference_analyzer import sample_reference_frames
        except ImportError:
            from style_reference_analyzer import sample_reference_frames
        return sample_reference_frames(str(ref), str(adir))   # count=None → 按参考时长自适应
    except Exception as e:  # noqa: BLE001 —— 抽帧失败不拖垮现写
        logger.warning(f"ArmB style: 现写参考帧抽取失败(忽略): {type(e).__name__}: {e}")
        return []


def _analyze_style_layer(job_dir, out_dir, ref, aspects, *, window=None,
                         focus_hint=None, cache_prefix="style_spec",
                         cost_source="style_reference"):
    """跑一层分析(整体或聚焦)→ (spec, frames)。带 aspects(+window)指纹缓存、原子落盘、
    仅缓存 miss 记账(按 cost_source 区分两层)。失败/空谱 → (空谱, [])。永不抛。"""
    try:
        from .style_reference import empty_style_spec, is_empty_style_spec
        from .style_reference_analyzer import analyze_reference
    except ImportError:
        from style_reference import empty_style_spec, is_empty_style_spec
        from style_reference_analyzer import analyze_reference
    # 缓存键 = 维度 +(有窗时)窗;窗不同 → 不同缓存文件,整体/聚焦互不污染。
    key_src = ",".join(sorted(aspects))
    if window is not None:
        try:
            key_src += f"|w={float(window[0]):.2f}-{float(window[1]):.2f}"
        except (TypeError, ValueError, IndexError):
            pass
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:8]
    cache = Path(out_dir) / f"{cache_prefix}_{key}.json"
    if cache.exists():
        try:
            spec = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(spec, dict) and not is_empty_style_spec(spec):
                return spec, [f for f in (spec.get("source_frames") or []) if f]
        except Exception:  # noqa: BLE001 —— 缓存坏了就重算
            pass
    sub = ".style_focus" if window is not None else ".style_ref"
    spec = analyze_reference(str(ref), aspects, out_dir=str(Path(out_dir) / sub),
                             window=window, focus_hint=focus_hint)
    if not is_empty_style_spec(spec):
        try:
            tmp = cache.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(cache))   # 原子落盘,防并发读到半截
        except Exception:  # noqa: BLE001 —— 缓存写失败不影响本次使用
            pass
        _record_style_cost(job_dir, spec.get("cost_usd"), source=cost_source)
        logger.info(f"ArmB style[{cost_source}]: 谱已生成(mode={spec.get('analysis_mode')}, "
                    f"cost=${float(spec.get('cost_usd') or 0):.4f}, aspects={spec.get('aspects')}, "
                    f"window={window})")
        return spec, [f for f in (spec.get("source_frames") or []) if f]
    logger.info(f"ArmB style[{cost_source}]: 未得可用谱(corrections={spec.get('corrections')})")
    return spec, []


def _load_style_specs(job, job_dir: Path, out_dir: Path, instruction: str, feedback: str = ""):
    """块④:参考素材 style_ref.* → 整体谱 +(可选)聚焦谱。返回 dict:
        global_on/global_spec/global_frames, focus_spec/focus_frames, focus_window。
    用 plan_style_analysis 编排(见模块1):
      · 无窗 → 只整体(等价旧行为)。
      · 有窗 → 必做聚焦(只喂参考的那一小段);rest 表达"也要整体风格"时叠加整体。
    整体/聚焦各自独立缓存、各自记账。维度由"原始指令 + 本轮修订反馈"共同决定。永不抛。"""
    empty = {"global_on": False, "global_spec": {}, "global_frames": [],
             "focus_spec": {}, "focus_frames": [], "focus_window": None}
    try:
        try:
            from .style_reference import plan_style_analysis, empty_style_spec
        except ImportError:
            from style_reference import plan_style_analysis, empty_style_spec
    except Exception as e:  # noqa: BLE001 —— 模块1 未就位:退化为"不参照"
        logger.warning(f"ArmB style: 参考风格模块未就位,跳过参照: {type(e).__name__}: {e}")
        return empty
    try:
        ref = _resolve_reference(job_dir)
        if ref is None:
            return {"global_on": True, "global_spec": empty_style_spec(), "global_frames": [],
                    "focus_spec": {}, "focus_frames": [], "focus_window": None}
        plan = plan_style_analysis((str(instruction) + " " + str(feedback)).strip())
        out = {"global_on": bool(plan.get("global_on")),
               "global_spec": empty_style_spec(), "global_frames": [],
               "focus_spec": {}, "focus_frames": [], "focus_window": plan.get("window")}
        if plan.get("global_on"):
            gspec, gframes = _analyze_style_layer(
                job_dir, out_dir, ref, plan["global_aspects"],
                cache_prefix="style_spec", cost_source="style_reference")
            out["global_spec"], out["global_frames"] = gspec, gframes
        if plan.get("focus_on") and plan.get("window") is not None:
            win = plan["window"]
            try:
                hint = f"t={float(win[0]):.0f}-{float(win[1]):.0f}s"
            except (TypeError, ValueError, IndexError):
                hint = None
            fspec, fframes = _analyze_style_layer(
                job_dir, out_dir, ref, plan["focus_aspects"], window=win,
                focus_hint=hint, cache_prefix="style_focus",
                cost_source="style_reference_focus")
            out["focus_spec"], out["focus_frames"] = fspec, fframes
        return out
    except Exception as e:  # noqa: BLE001 —— 参考分析绝不拖垮现写
        logger.warning(f"ArmB style: 参考分析异常,跳过参照: {type(e).__name__}: {e}")
        return empty


def _load_style_spec(job, job_dir: Path, out_dir: Path, instruction: str, feedback: str = ""):
    """向后兼容薄封装:只返回整体层 (spec, frames)。新代码请用 _load_style_specs。"""
    s = _load_style_specs(job, job_dir, out_dir, instruction, feedback)
    return s["global_spec"], s["global_frames"]


def _tok(s: str) -> list:
    import re
    return [w for w in re.split(r"[^0-9a-z一-鿿]+", str(s or "").lower()) if w]


def _match_tokens(label: str) -> list:
    """用于 label↔转写匹配的词:英文≥3 字(滤 the/a 噪声),CJK≥2 字(中文双字词就算
    有意义;原来一刀切 len>=3 把所有中文双字词砍光了)。"""
    out = []
    for w in _tok(label):
        is_cjk = any("一" <= ch <= "鿿" for ch in w)
        if (is_cjk and len(w) >= 2) or (not is_cjk and len(w) >= 3):
            out.append(w)
    return out


def _assign_broll_windows(broll: list, segments: list, duration_s: float,
                          fps: int = 30, win_len_s: float = 4.0, min_gap_s: float = 0.4) -> list:
    """给**没有有效时间窗**(endFrame<=startFrame)的 b-roll 分配真实窗口——否则 props
    里是 0/0 空窗,模型渲 0 帧=上传的 b-roll 根本不出现(author-first 目录兜底就是 0/0)。

    先给每片算一个"期望中心":label 与转写分句词重叠命中→贴那句;否则均匀分布。
    再按期望中心排序**贪心不重叠**放置(游标推进 = 上一窗尾 + min_gap),保证多段 b-roll
    互不遮挡(修对抗审查发现的"同分句/间距不足→窗口重叠或相同"的病)。已带窗的不动。
    就地补 startFrame/endFrame(int 帧)+ 标 auto_window,返回同一列表。"""
    if duration_s <= 0:
        return broll
    windowless = [b for b in broll
                  if not (int(b.get("endFrame") or 0) > int(b.get("startFrame") or 0))]
    if not windowless:
        return broll
    n = len(windowless)
    pad = min(1.0, duration_s * 0.05)
    usable = max(0.0, duration_s - 2 * pad)
    if usable <= 0:                        # 极短视频:整段给它们(退化但不崩/不越界)
        for b in windowless:
            b["startFrame"], b["endFrame"] = 0, max(1, int(round(duration_s * fps)))
            b["auto_window"] = True
        return broll
    # 窗长:留出 (n-1) 个间隔,保证 n 段能塞进 usable 而不重叠;下限 1.0s
    L = max(1.0, min(win_len_s, (usable - (n - 1) * min_gap_s) / n))

    def _anchor_center(b, idx: int) -> float:
        toks = _match_tokens(str(b.get("label", "")))
        best, best_score = None, 0
        for s in segments or []:
            text = str(s.get("text", "")).lower()
            score = sum(1 for t in toks if t in text)
            if score > best_score:
                best_score, best = score, s
        if best is not None and best_score > 0:
            try:
                return float(best.get("start", 0.0)) + L / 2   # 分句起点 → 窗口中心
            except (TypeError, ValueError):
                pass
        return pad + usable * (idx + 0.5) / n                   # 均匀分布中心

    right = pad + usable
    ordered = sorted(((_anchor_center(b, i), b) for i, b in enumerate(windowless)),
                     key=lambda x: x[0])
    cursor = pad
    for center, b in ordered:
        t0 = max(cursor, center - L / 2)   # 不早于游标(防重叠),尽量贴期望中心
        t0 = max(pad, min(t0, right - L))  # 夹在 [pad, right-L]
        t1 = min(right, t0 + L)
        b["startFrame"] = int(round(t0 * fps))
        b["endFrame"] = int(round(t1 * fps))
        b["auto_window"] = True
        cursor = t1 + min_gap_s
    return broll


# ─────────────────────────── 生成型 b-roll(方案B)───────────────────────────
# 让 AI 现写路径也能"生成一段 b-roll":plan/revise 抽意图(成功后才持久化),compose 才真生成。

# 关键词门:同时命中"生成类动词 + b-roll类词"才调 LLM。刻意收紧,避免"提亮画面/加素材"
# 这类普通指令误触发一次付费 LLM 调用(对抗审查 MEDIUM #4)。
_GEN_VERBS = ("生成", "新做", "新生成", "重新生成", "generate")
_BROLL_WORDS = ("broll", "b-roll", "b roll", "空镜", "空镜头", "footage", "视频片段")


def _gen_broll_enabled() -> bool:
    return os.getenv("ARM_B_GEN_BROLL", "1").strip().lower() in ("1", "true", "yes", "on")


def _gen_broll_json(out_dir):
    return Path(out_dir) / "gen_broll.json"


def _read_gen_broll(out_dir) -> list:
    p = _gen_broll_json(out_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _write_gen_broll(out_dir, reqs) -> None:
    """现写/修订成功后才落盘 gen_broll.json(bail 时不动它 → 旧草稿与旧 json 保持一致)。"""
    try:
        _gen_broll_json(out_dir).write_text(
            json.dumps(reqs or [], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ArmB gen-broll: 持久化失败(忽略): {type(e).__name__}: {e}")


_GEN_BROLL_SYS = (
    "You extract requests to GENERATE brand-new b-roll footage with AI from a video-editing "
    "instruction. ONLY extract explicit requests to CREATE/GENERATE new footage (e.g. '生成一段…的"
    "b-roll', 'AI 做一段空镜', 'generate a clip of …'). Do NOT extract references to clips the user "
    "ALREADY uploaded (e.g. '视频3是b-roll', 'insert the uploaded clip') — those are not generation. "
    "For each generation request output:\n"
    "  prompt = a concise ENGLISH text-to-video generation prompt describing the desired footage;\n"
    "  cue    = the words/topic the speaker is saying when this b-roll should appear (copy the "
    "relevant phrase from the instruction or transcript).\n"
    "Return ONLY a JSON array (possibly empty []), no prose, of shape "
    "[{\"prompt\": \"...\", \"cue\": \"...\"}]."
)


def _extract_gen_broll(instruction, segments, llm_call=None) -> list:
    """从指令抽"要 AI 生成新 b-roll"的请求(方案B)。返回 [{prompt, cue, provider}]。
    只有指令里同时出现"生成类动词 + b-roll类词"才调 LLM;永不抛,失败/无意图/开关关/未配
    模型 → []。上限 ARM_B_GEN_BROLL_MAX。"""
    if not _gen_broll_enabled():
        return []
    text = str(instruction or "").strip()
    tl = text.lower()
    if not (any(v in tl for v in _GEN_VERBS) and any(w in tl for w in _BROLL_WORDS)):
        return []
    try:
        try:
            from .scene_author import _default_llm_call
        except ImportError:
            from scene_author import _default_llm_call
        fn = llm_call or _default_llm_call
    except Exception:  # noqa: BLE001
        return []
    seg_txt = "\n".join(
        f"[{float(s.get('start', 0) or 0):.1f}s] {str(s.get('text', '')).strip()}"
        for s in (segments or [])[:80])
    user = (f"USER INSTRUCTION:\n{text}\n\nTRANSCRIPT (for cue matching):\n{seg_txt}\n\n"
            "Return ONLY the JSON array.")
    try:
        raw = fn([{"role": "system", "content": _GEN_BROLL_SYS},
                  {"role": "user", "content": user}], 4000, 0.2)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ArmB gen-broll: 抽取调用失败(忽略): {type(e).__name__}: {e}")
        return []
    txt = str((raw or {}).get("content", "")).strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.rstrip().endswith("```"):
            txt = txt.rstrip()[:-3]
    obj = None
    try:
        obj = json.loads(txt)
    except Exception:  # noqa: BLE001 —— 截中括号再试
        s, e = txt.find("["), txt.rfind("]")
        if 0 <= s < e:
            try:
                obj = json.loads(txt[s:e + 1])
            except Exception:  # noqa: BLE001
                obj = None
    if not isinstance(obj, list):
        return []
    out = []
    prov = os.getenv("ARM_B_GEN_BROLL_PROVIDER", "omni")
    for it in obj:
        if isinstance(it, dict) and str(it.get("prompt", "")).strip():
            out.append({"prompt": str(it["prompt"]).strip()[:500],
                        "cue": str(it.get("cue", "")).strip()[:200],
                        "provider": prov})
    try:
        cap = int(os.getenv("ARM_B_GEN_BROLL_MAX", "2"))
    except ValueError:
        cap = 2
    return out[:max(0, cap)]


def _gen_broll_entries(job_dir, reqs) -> list:
    """把抽取到的 reqs 列表登记成 b-roll 待生成条目(文件此刻可能还不在)。窗给 0/0,交
    _assign_broll_windows 按 label(cue) 匹配转写定位。key=gen<i> → assets/broll_gen<i>.mp4。"""
    out = []
    for i, it in enumerate(reqs or []):
        if not isinstance(it, dict) or not str(it.get("prompt", "")).strip():
            continue
        key = f"gen{i}"
        out.append({
            "src": str(Path(job_dir) / "assets" / f"broll_{key}.mp4"),
            "label": str(it.get("cue") or it.get("prompt"))[:120],
            "startFrame": 0, "endFrame": 0,
            "_gen_prompt": str(it["prompt"]),
            "_gen_provider": str(it.get("provider") or os.getenv("ARM_B_GEN_BROLL_PROVIDER", "omni")),
            "_gen_key": key,
        })
    return out


def _gen_broll_retry_backoffs() -> list:
    """gen-broll 生成失败(尤其 provider 瞬时 500 "high demand" / 429 限流)的退避重试秒序列。
    默认 5,15,30(共 3 次重试);env ARM_B_GEN_BROLL_RETRY 覆盖,设 "" 或 "0" 关闭真 sleep。
    总退避落在 compose 阶段的长超时预算内。"""
    raw = os.getenv("ARM_B_GEN_BROLL_RETRY", "5,15,30")
    out = []
    for x in str(raw).split(","):
        try:
            v = float(x.strip())
        except ValueError:
            continue
        if v >= 0:
            out.append(v)
    return out


def _materialize_gen_broll(broll, job_dir):
    """compose 阶段(confirm 后)真正生成待办 b-roll。对每条带 _gen_prompt 且文件不在的 →
    generate_broll_via 落文件 + 记账(broll_gen[key]);已存在则复用(revise 不重复生成)。
    失败按 _gen_broll_retry_backoffs 退避重试(扛 provider 瞬时 500/限流);最终仍失败 →
    从列表摘掉该条(现写 tsx 遇无 b-roll 会画占位卡)并计入 failed(供上层告知用户)。
    返回 (存活列表, 最终失败的生成条目列表)。永不抛。"""
    import time
    if not _gen_broll_enabled():
        return ([b for b in (broll or []) if not b.get("_gen_prompt")], [])
    out, failed = [], []
    delays = _gen_broll_retry_backoffs()
    for b in broll or []:
        prompt = b.get("_gen_prompt")
        if not prompt:
            out.append(b)
            continue
        src = Path(b.get("src", ""))
        if src.exists() and src.stat().st_size > 0:
            out.append(b)                          # 缓存命中,不重生成
            continue
        res = None
        for attempt in range(len(delays) + 1):     # 首次 + len(delays) 次重试
            try:
                try:
                    from ..broll_providers import generate_broll_via
                except ImportError:
                    from broll_providers import generate_broll_via
                src.parent.mkdir(parents=True, exist_ok=True)
                aspect = str(b.get("_gen_aspect") or "9:16")
                res = generate_broll_via(b.get("_gen_provider", "omni"), str(prompt), str(src), aspect=aspect)
            except Exception as e:  # noqa: BLE001 —— 生成绝不拖垮现写
                logger.warning(f"ArmB gen-broll: 生成异常: {type(e).__name__}: {e}")
                res = None
            if res and src.exists() and src.stat().st_size > 0:
                break
            if attempt < len(delays):
                logger.warning(f"ArmB gen-broll: 生成失败,{delays[attempt]:.0f}s 后重试第 {attempt + 1} 次"
                               f"(key={b.get('_gen_key', '?')})")
                if delays[attempt] > 0:
                    time.sleep(delays[attempt])
        if res and src.exists() and src.stat().st_size > 0:
            _record_style_cost(job_dir, (res or {}).get("cost_usd"),
                               source=f"broll_gen[{b.get('_gen_key', '?')}]")
            logger.info(f"ArmB gen-broll: 已生成 {src.name}(cost=${float((res or {}).get('cost_usd') or 0):.4f})")
            out.append(b)
        else:
            logger.warning(f"ArmB gen-broll: 生成最终失败,该段降级为占位(prompt={str(prompt)[:40]!r})")
            failed.append(b)
    return (out, failed)


# ─────────────────────────── 公共准备 ───────────────────────────

def _prepare(job, feedback: str = "", *, extract_gen: bool = False):
    """落 authored/ 目录、取转写、收 b-roll、建 AuthorContext。失败返回 None。
    plan_authored 与 compose_authored 共用,保证两处的 ctx 完全一致。"""
    from ..config import get_config
    config = get_config()
    job_dir = Path(job.job_dir)
    input_video = job_dir / "input.mp4"
    if not input_video.exists():
        return None
    out_dir = job_dir / "authored"
    out_dir.mkdir(parents=True, exist_ok=True)
    t = _get_transcript(job, str(input_video))
    if t is None:
        return None
    words = t.get("word_timestamps") or []
    segments = t.get("segments") or []
    duration = float(t.get("duration_seconds") or 0) or _probe_duration(input_video)
    if duration <= 0:
        return None
    # 指令源(提前到收 b-roll 前:生成型 b-roll 抽取要用它)。edit_request 才是真实字段
    # (job.request 在 DB Job 上不存在,之前恒为空 → 模型从没拿到指令,2026-07-27 修)。
    instruction = str(getattr(job, "edit_request", "") or getattr(job, "request", "")
                      or getattr(job, "instruction", "") or "")
    broll = _collect_broll(job)
    # 生成型 b-roll(方案B):plan/revise(extract_gen=True)抽意图 → 内存 reqs;compose
    # (extract_gen=False)读上次成功落盘的 gen_broll.json。reqs 随 ctx 返回,由调用方在
    # 现写/修订【成功后】才 _write_gen_broll(避免 revise 失败保留旧草稿、却已改写 json)。
    gen_reqs = (_extract_gen_broll((instruction + " " + (feedback or "")).strip(), segments)
                if extract_gen else _read_gen_broll(out_dir))
    broll = broll + _gen_broll_entries(job_dir, gen_reqs)
    # 给没窗的 b-roll(上传/生成走 0/0)分配真实时间窗,模型才会真的合成它。
    broll = _assign_broll_windows(broll, segments, duration)
    # 模块5:参考素材 style_ref.*(模块4 落盘)→ StyleSpec + 代表帧。空谱等于"不参照",
    # scene_author 见空谱不加"参考风格"段,照常出片。代表帧(抽帧/图片模式)并入参考图,
    # 让模型除了文字风格谱外还能"看到"参考画面。
    # 块④:两层编排——整体谱 + (可选)聚焦谱。plan_style_analysis 决定是否/怎么分层。
    specs = _load_style_specs(job, job_dir, out_dir, instruction, feedback)
    style_spec = specs["global_spec"]
    # 整体参考帧只在"做了整体分析"时给;focus-only 模式不给整段参考帧(免得又去模仿整体)。
    author_frames = _author_frames(job_dir, out_dir, style_spec) if specs["global_on"] else []
    example_images = _style_refs() + [f for f in author_frames if f and Path(f).exists()]
    focus_images = [f for f in specs["focus_frames"] if f and Path(f).exists()]
    ctx = AuthorContext(segments=segments, words=words, duration_s=duration,
                        instruction=instruction, example_images=example_images,
                        broll=broll, style_spec=style_spec,
                        focus_spec=specs["focus_spec"], focus_images=focus_images,
                        focus_window=specs["focus_window"])
    return {"config": config, "job_dir": job_dir, "input_video": input_video,
            "out_dir": out_dir, "words": words, "duration": duration,
            "broll": broll, "ctx": ctx, "gen_reqs": gen_reqs}


class _DraftResult:
    """把已 author 好的 scene_draft.tsx 包成 author_fn 的返回形状,喂进 M7 状态机
    (让 confirm 后的渲染直接从草稿走 validate→render→qa,不再花一次 author 调用)。"""
    def __init__(self, tsx: str):
        self.ok = bool(tsx.strip())
        self.tsx = tsx
        self.usage = {}
        self.contract_markers = self.ok
        self.error = "" if self.ok else "scene_draft.tsx 为空"


def _scene_narrative(tsx=None, storyboard_skeleton=None) -> dict:
    """emit_storyboard 的 narrative_fn 适配器:读当前 tsx → 画面设计描述 {summary,style}。
    describe_scene 失败返回 {},M4 会自动降级为纯 derived 分镜。以模块级函数暴露,便于测试打桩。"""
    return describe_scene(tsx) if tsx else {}


def _emit_storyboard_files(out_dir: Path, words: list, broll: list, duration: float,
                           tsx: str | None = None) -> str:
    # 有 tsx 且开关未关 → 让模型描述画面设计(卡片颜色/特效/PIP 等),使颜色/样式类
    # 改动也能在分镜文本里体现;否则纯 derived(时间轴+字幕+b-roll)。失败自动降级。
    narr = _scene_narrative if (tsx and os.getenv("ARM_B_STORYBOARD_NARRATIVE", "1") == "1") else None
    sb = emit_storyboard(words, broll, duration, narrative_fn=narr, tsx=tsx)
    (out_dir / "storyboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")
    text = render_text(sb)
    (out_dir / "storyboard.md").write_text(text, encoding="utf-8")
    return text


def plan_authored(job) -> dict | None:
    """补丁点①:author 先行。arm_b 路径在**规划阶段**(confirm 之前)就现写 tsx、
    过安全闸(必要时修一轮)、落 scene_draft.tsx、出**分镜当方案**。返回可写进
    planned_edit 的 dict;失败返回 None → 调用方落穿现有 L2 规划(Arm A)。
    不渲染——渲染留到 confirm 之后由 compose_authored 认草稿来做。"""
    try:
        p = _prepare(job, extract_gen=True)
        if p is None:
            return None
        ctx, out_dir = p["ctx"], p["out_dir"]
        r = author_scene(ctx)
        if r.ok:
            v = validate_tsx(r.tsx)
            if not getattr(v, "ok", False):
                from .compose_orchestrator import _violations_to_defects
                rr = revise_scene(r.tsx, _violations_to_defects(getattr(v, "violations", [])),
                                  ctx, "代码未过安全/契约校验,按违规逐条修正,别的不动。")
                if rr.ok:
                    r = rr
        if not r.ok or not r.tsx.strip():
            logger.warning(f"ArmB plan: author 未过({r.error}),落穿 L2 规划")
            return None
        (out_dir / "scene_draft.tsx").write_text(r.tsx, encoding="utf-8")
        _write_gen_broll(out_dir, p.get("gen_reqs"))   # 现写成功才落盘 → compose 读到与草稿一致
        summary = _emit_storyboard_files(out_dir, p["words"], p["broll"], p["duration"], tsx=r.tsx)
        logger.info(f"ArmB plan: 已现写 scene_draft.tsx({len(r.tsx)} 字符),分镜当方案")
        return {"arm_b": True, "summary": summary,
                "edit_operations": [{"type": "authored_compose",
                                     "description": "按上面的分镜用 AI 现写场景渲染(Arm B)"}]}
    except Exception as e:  # noqa: BLE001 —— 规划阶段不许炸,落穿 L2
        logger.warning(f"ArmB plan: 异常落穿 L2: {type(e).__name__}: {e}")
        return None


def revise_authored_plan(job, feedback: str) -> dict | None:
    """补丁点②:确认前改草稿。用户在 WAITING_CONFIRMATION 阶段发反馈时,arm_b 路径
    **不重跑 L2 规划**,而是对已落盘的 scene_draft.tsx 跑 revise_scene(把用户反馈当
    修订指令),过 M1(必要时按违规再修一轮),覆盖草稿、重出分镜,返回可写进
    planned_edit 的 dict → 调用方 _send_confirmation 回到 WAITING_CONFIRMATION。
    **不渲染**——渲染仍留到用户确认后由 compose_authored 认新草稿来做。

    返回 None 的情形(调用方落穿现有 L2 就地修订):无草稿(没走过 author 先行)、
    _prepare 失败、revise 未产出合法 tsx、或任何异常。"""
    try:
        p = _prepare(job, feedback, extract_gen=True)   # 反馈并入风格维度解析 + 生成型 b-roll 抽取
        if p is None:
            return None
        ctx, out_dir = p["ctx"], p["out_dir"]
        draft = out_dir / "scene_draft.tsx"
        if not (draft.exists() and draft.read_text(encoding="utf-8").strip()):
            logger.info("ArmB revise: 无 scene_draft.tsx(未走 author 先行),落穿 L2 就地修订")
            return None
        cur = draft.read_text(encoding="utf-8")
        notes = f"用户对上一版的修改意见(务必据此改,其余保持不动):{feedback}"
        r = revise_scene(cur, [], ctx, notes)
        if r.ok:
            v = validate_tsx(r.tsx)
            if not getattr(v, "ok", False):
                from .compose_orchestrator import _violations_to_defects
                rr = revise_scene(r.tsx, _violations_to_defects(getattr(v, "violations", [])),
                                  ctx, "上一版改动引入了安全/契约违规,按违规逐条修正,别的不动。")
                if rr.ok:
                    r = rr
        if not r.ok or not r.tsx.strip():
            logger.warning(f"ArmB revise: 未产出合法草稿({r.error}),落穿 L2 就地修订")
            return None
        v2 = validate_tsx(r.tsx)
        if not getattr(v2, "ok", False):
            logger.warning("ArmB revise: 修订后仍未过 M1,保留旧草稿,落穿 L2")
            return None
        draft.write_text(r.tsx, encoding="utf-8")   # 覆盖草稿 → confirm 后渲这版
        _write_gen_broll(out_dir, p.get("gen_reqs"))   # 修订成功才落盘 → 与新草稿一致
        summary = _emit_storyboard_files(out_dir, p["words"], p["broll"], p["duration"], tsx=r.tsx)
        logger.info(f"ArmB revise: 已按反馈改草稿({len(r.tsx)} 字符),分镜当方案,未渲染")
        return {"arm_b": True, "summary": summary,
                "edit_operations": [{"type": "authored_compose",
                                     "description": "按修订后的分镜用 AI 现写场景渲染(Arm B)"}]}
    except Exception as e:  # noqa: BLE001 —— 修订阶段不许炸,落穿 L2
        logger.warning(f"ArmB revise: 异常落穿 L2: {type(e).__name__}: {e}")
        return None


# ─────────────────────────── 主入口 ───────────────────────────

def compose_authored(job) -> dict | None:
    try:
        return _compose_authored_inner(job)
    except Exception as e:  # noqa: BLE001 —— 门内永不抛,任何意外都落穿
        logger.warning(f"ArmB: 未预期异常,落穿 Arm A: {type(e).__name__}: {e}")
        return None


def _compose_authored_inner(job) -> dict | None:
    p = _prepare(job)
    if p is None:
        return None
    config, job_dir, input_video = p["config"], p["job_dir"], p["input_video"]
    out_dir, words, duration, broll, ctx = (
        p["out_dir"], p["words"], p["duration"], p["broll"], p["ctx"])

    # confirm 后:真正生成待办 b-roll(几分钟,落在本阶段的长超时里);失败的摘掉,
    # 现写 tsx 遇无 b-roll 会画占位卡。ctx.broll 同步更新,渲染 props 与之一致。
    broll, _gen_failed = _materialize_gen_broll(broll, job_dir)
    ctx.broll = broll

    rc_dir = Path(config.openmontage_root) / "remotion-composer"
    timeout_s = _env_int("ARM_B_RENDER_TIMEOUT_S", 600)
    render_no = {"n": 0}

    def render_fn(tsx: str):
        render_no["n"] += 1
        (out_dir / f"scene_r{render_no['n']}.tsx").write_text(tsx, encoding="utf-8")
        out_path = out_dir / f"render_r{render_no['n']}.mp4"
        try:
            from ..concurrency import RENDER_SLOTS   # 尊重单机重活并发闸
            with RENDER_SLOTS:
                return render_authored(tsx, input_video, broll, words, duration,
                                       out_path, rc_dir=rc_dir, timeout_s=timeout_s)
        except ImportError:
            return render_authored(tsx, input_video, broll, words, duration,
                                   out_path, rc_dir=rc_dir, timeout_s=timeout_s)

    def qa_fn(mp4_path):
        return qa_render(Path(mp4_path), expected_duration_s=duration,
                         evidence_dir=out_dir / "qa")

    def storyboard_fn(tsx: str):
        # 渲染后(confirm 之后)重出分镜:不再调 describe(用户在 plan/revise 阶段已看过画面
        # 描述,这里再调一次纯属重复消费,徒增一次 32000 预算调用)——纯 derived 即可。
        return {"summary": _emit_storyboard_files(out_dir, words, broll, duration)}

    # 补丁点③:规划阶段若已 author 出 scene_draft.tsx(author 先行),confirm 后直接
    # 从草稿进 validate→render→qa,省掉一次 author 调用;否则(如未走 author 先行、
    # 或直接调 compose)现场 author。
    draft = out_dir / "scene_draft.tsx"
    if draft.exists() and draft.read_text(encoding="utf-8").strip():
        _draft_tsx = draft.read_text(encoding="utf-8")
        author_fn = lambda _ctx: _DraftResult(_draft_tsx)   # noqa: E731
        logger.info("ArmB compose: 复用规划阶段的 scene_draft.tsx(不重复 author)")
    else:
        author_fn = author_scene

    budget = ComposeBudget(
        max_revise_rounds=_env_int("ARM_B_MAX_ROUNDS", 1),
        wallclock_budget_s=float(_env_int("ARM_B_RENDER_TIMEOUT_S", 600)) * 3,
        cost_ceiling_usd=_env_float("ARM_B_COST_CEILING", 0.30))

    rep = compose(ctx,
                  author_fn=author_fn,
                  validate_fn=validate_tsx,
                  render_fn=render_fn,
                  qa_fn=qa_fn,
                  revise_fn=revise_scene,
                  storyboard_fn=storyboard_fn,
                  fallback_fn=None,          # 兜底=调用方落穿现有 Arm A 代码体
                  budget=budget)

    report = {"status": rep.status, "rounds": rep.rounds,
              "cost_usd": round(rep.cost_usd, 4), "qa": rep.qa,
              "fell_back": rep.fell_back, "error": rep.error, "history": rep.history}
    (out_dir / "compose_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if rep.status not in ("accepted", "accepted_best") or not rep.mp4:
        logger.warning(f"ArmB: 未出片(status={rep.status}),落穿 Arm A")
        return None

    preview = job_dir / "preview.mp4"
    shutil.copyfile(rep.mp4, preview)
    logger.info(f"=== ArmB 出片: {job.id} → {preview} "
                f"(status={rep.status}, rounds={rep.rounds}, cost=${rep.cost_usd:.4f}) ===")
    # 返回与 Arm A 主返回同构的关键字段;验收时如发现下游还消费其它键,在此补齐
    # 生成型 b-roll 若重试后仍失败,通过 degraded_operations 告知用户:worker.js 的
    # previewReadyMessage 会渲染 ⚠️「AI b-roll」这一步执行失败、当前预览不含该效果、
    # 可回复 retry 重跑。与 Arm A 同键(status.degraded_operations)。
    _degraded = ["AI b-roll"] if _gen_failed else []
    return {"preview_path": str(preview),
            "duration_seconds": duration,
            "applied": ["authored_compose"],
            "degraded": _degraded,
            "degraded_operations": _degraded,
            "compose_arm": "arm_b",
            "authored_report": report}


def _probe_duration(p: Path) -> float:
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0