#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块2 · StyleReferenceAnalyzer(参考分析器,设计文档 §3.2)。

输入 = 参考素材路径 + 选中维度(来自模块1)→ 输出 = StyleSpec(模块1 契约)。
三条子路 + 降级(§2c):
  - 图片          → 视觉模型看图(OpenAI-compat)
  - 视频 ≤阈值    → Gemini 原生视频路径(video_call)
  - 视频 >阈值 / 原生失败 → 抽关键帧(自实现 ffmpeg,不用有 bug 的 frame_sampler)喂视觉模型
  - 任何一步全失败 → 返回空 StyleSpec(Arm B 照常出片,只是不参照)。**永不抛异常。**

所有外部依赖可注入,容器可全测:
  video_call(path, prompt) -> {"content","usage"}          # Gemini 原生视频
  vision_call(messages, max_tokens, temperature) -> {...}  # OpenAI-compat 视觉(图/帧)
  sampler(video_path, out_dir, count) -> [frame_path,...]   # 抽帧
不传则用默认实现(真到集成时按第1条规矩实测端点/装 ffmpeg)。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path

try:  # 包内=相对导入;容器独测=裸导入
    from .style_reference import ASPECTS, DEFAULT_ASPECTS, empty_style_spec
except ImportError:
    from style_reference import ASPECTS, DEFAULT_ASPECTS, empty_style_spec

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

MAX_TOKENS = 32000        # 思考型模型 thinking 与输出共用预算(同 scene_author)
TEMP = 0.2

# 每个维度让模型回什么(引导 JSON 形状)
_ASPECT_HINT = {
    "pacing":      '"pacing": {"avg_shot_s": <数字>, "cut_density": "low|medium|high", "rhythm": "<一句>"}',
    "transitions": '"transitions": [{"type": "<如 hard_cut/cross_dissolve/whip_pan>", "desc": "<一句>"}]',
    "animation":   '"animation": [{"element": "<元素>", "motion": "<动作>", "easing": "<缓动>"}]',
    "camera":      '"camera": [{"move": "<如 push_in/pan/handheld>", "desc": "<一句>"}]',
    "color":       '"color": {"palette": ["#RRGGBB"], "grade": "<一句>", "mood": "<一句>"}',
    "typography":  '"typography": {"style": "<字体/字重>", "position": "<位置>", "treatment": "<处理>"}',
    "graphics":    '"graphics": {"cards": "<卡片样式>", "layout": "<版式>"}',
}


def _usage_norm(raw) -> dict:
    """把原生 Gemini(usageMetadata:promptTokenCount/candidatesTokenCount/totalTokenCount)
    与 OpenAI-compat(usage:prompt_tokens/completion_tokens/total_tokens)两种用量字段
    归一成 {prompt, completion, total}(整数 token)。"""
    u = raw or {}
    prompt = u.get("prompt_tokens", u.get("promptTokenCount"))
    completion = u.get("completion_tokens", u.get("candidatesTokenCount"))
    total = u.get("total_tokens", u.get("totalTokenCount"))
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    total = int(total or 0) or (prompt + completion)
    return {"prompt": prompt, "completion": completion, "total": total}


def _cost_from(usage: dict) -> float:
    """按 env 单价估算本次分析花费($)。thinking 计输出价:billable_out=total-prompt
    (与 scene_author.cost_usd 口径一致)。默认单价 = gemini-2.5-flash-lite($0.10/$0.40 每 1M)。"""
    try:
        price_in = float(os.getenv("STYLEREF_PRICE_IN", "0.10"))
        price_out = float(os.getenv("STYLEREF_PRICE_OUT", "0.40"))
    except ValueError:
        price_in, price_out = 0.10, 0.40
    prompt = usage.get("prompt", 0)
    billable_out = max(0, usage.get("total", 0) - prompt)
    return prompt / 1e6 * price_in + billable_out / 1e6 * price_out


def _attach_cost(spec: dict, raw: dict) -> None:
    """把一次调用的 usage/cost 记进 spec(成功子路调用)。"""
    spec["usage"] = _usage_norm((raw or {}).get("usage"))
    spec["cost_usd"] = round(_cost_from(spec["usage"]), 6)


def _clean_aspects(aspects) -> list:
    keep = [a for a in (aspects or []) if a in ASPECTS]
    return keep or list(DEFAULT_ASPECTS)


def _prompt(aspects: list, focus_hint=None) -> str:
    """聚焦选中维度的分析指令,要求严格 JSON。focus_hint 非空时,提示模型这是用户
    单独点名的一小段,要重点描述该片段里发生的【具体视觉效果】,详到别人能复刻。"""
    lines = "\n".join("  " + _ASPECT_HINT[a] for a in aspects)
    if focus_hint:
        head = (
            "You are a video-editing style analyst. The frames/clip below are a SHORT SEGMENT "
            "the user singled out (" + str(focus_hint) + "). Describe in DETAIL the SPECIFIC "
            "visual EFFECT / graphic / motion happening in this segment — enough for another "
            "editor to REPRODUCE it — covering only the dimensions listed below, in Chinese, "
            "concise. Describe the EFFECT/STYLE only; do NOT transcribe spoken/textual CONTENT.\n"
        )
    else:
        head = (
            "You are a video-editing style analyst. Study the reference media and describe its "
            "EDITING/VISUAL STYLE — only the dimensions listed below, in Chinese, concise. "
            "Describe STYLE only; do NOT transcribe or summarize its spoken/textual CONTENT.\n"
        )
    return (
        head +
        "Return ONLY compact JSON, no markdown, of this exact shape (include ONLY these keys "
        "plus \"overall\"):\n{\n"
        '  "overall": "<一句总体风格>",\n' + lines + "\n}"
    )


def _strip_fences(t: str) -> str:
    t = (t or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _parse_spec(content: str, aspects: list, corrections: list) -> dict | None:
    """把模型输出解析成 StyleSpec(只收选中维度 + overall)。失败返回 None。容错同 describe_scene。"""
    txt = _strip_fences(content)
    if not txt:
        corrections.append("模型返回空内容")
        return None
    obj = None
    try:
        obj = json.loads(txt)
    except Exception:  # noqa: BLE001 —— 截大括号再试
        s, e = txt.find("{"), txt.rfind("}")
        if 0 <= s < e:
            try:
                obj = json.loads(txt[s:e + 1])
            except Exception:  # noqa: BLE001
                obj = None
    if isinstance(obj, list):
        obj = next((x for x in obj if isinstance(x, dict)), None)
    if not isinstance(obj, dict):
        corrections.append(f"无法解析 JSON,原文前120: {txt[:120]!r}")
        return None
    out: dict = {}
    if isinstance(obj.get("overall"), str):
        out["overall"] = obj["overall"].strip()[:120]
    for a in aspects:                       # 只收选中维度
        v = obj.get(a)
        if isinstance(v, (dict, list)) and v:
            out[a] = v
    return out or None


# ─────────────────────────── 默认外部实现(可注入替换)───────────────────────────

def _b64_data_url(p: Path) -> str:
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    return f"data:image/{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def _default_vision_call(messages: list, max_tokens: int, temperature: float) -> dict:
    """复用 scene_author 的 OpenAI-compat 传输(支持 image_url parts)。"""
    try:  # 包内=相对;容器独测=裸
        from .scene_author import _default_llm_call
    except ImportError:
        from scene_author import _default_llm_call
    return _default_llm_call(messages, max_tokens, temperature)


def _default_sampler(video_path: str, out_dir: str, count: int, window=None) -> list:
    """自实现抽帧:ffmpeg 均匀抽 count 帧(避开有 'strategy' bug 的 frame_sampler)。
    window=(start,end) 时只在该时间窗内均匀抽(聚焦某片段);越界/退化自动夹到 [0,dur]。
    真到集成需机器有 ffmpeg;失败返回 []。"""
    outd = Path(out_dir)
    outd.mkdir(parents=True, exist_ok=True)
    dur = _probe_duration(Path(video_path))
    if dur <= 0:
        return []
    lo, hi = 0.0, dur
    if window:
        try:
            ws, we = float(window[0]), float(window[1])
            lo = max(0.0, min(ws, dur))
            hi = max(0.0, min(we, dur))
            if hi <= lo:                 # 窗越界/退化 → 回退整段,别抽空
                lo, hi = 0.0, dur
        except (TypeError, ValueError, IndexError):
            lo, hi = 0.0, dur
    span = hi - lo
    frames = []
    for i in range(count):
        t = lo + span * (i + 0.5) / count
        fp = outd / f"frame_{i}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path, "-frames:v", "1",
                 "-vf", "scale=-2:'min(720,ih)'",   # 长边压到≤720(只降不升),省分析 token/带宽
                 "-q:v", "3", str(fp)],
                capture_output=True, timeout=30)
            if fp.exists() and fp.stat().st_size > 0:
                frames.append(str(fp))
        except Exception:  # noqa: BLE001
            continue
    return frames


def _adaptive_frame_count(dur) -> int:
    """按参考时长自适应帧数:~每 STYLEREF_SECS_PER_FRAME 秒 1 帧,夹在 [MIN,MAX]。
    短片少抽、长片多抽;有上限,免 token/成本失控。"""
    import math
    def _envf(k, d):
        try:
            return float(os.getenv(k, str(d)))
        except (TypeError, ValueError):
            return float(d)
    secs = _envf("STYLEREF_SECS_PER_FRAME", 5) or 5.0
    lo = max(1, int(_envf("STYLEREF_MIN_FRAMES", 4)))  # 下限≥1,防负值误配产出空/负帧数
    hi = int(_envf("STYLEREF_MAX_FRAMES", 12))
    if hi < lo:
        hi = lo
    try:
        d = float(dur or 0)
    except (TypeError, ValueError):
        d = 0.0
    n = math.ceil(d / secs) if d > 0 else lo
    return max(lo, min(hi, n))


def _focus_frame_count(window) -> int:
    """聚焦窗抽帧数:比整体更密(~每 STYLEREF_FOCUS_SECS_PER_FRAME 秒 1 帧),夹在
    [STYLEREF_FOCUS_MIN_FRAMES, STYLEREF_FOCUS_MAX_FRAMES]。窗无效 → 下限。"""
    import math
    def _envf(k, d):
        try:
            return float(os.getenv(k, str(d)))
        except (TypeError, ValueError):
            return float(d)
    secs = _envf("STYLEREF_FOCUS_SECS_PER_FRAME", 0.8) or 0.8
    lo = max(1, int(_envf("STYLEREF_FOCUS_MIN_FRAMES", 4)))
    hi = int(_envf("STYLEREF_FOCUS_MAX_FRAMES", 8))
    if hi < lo:
        hi = lo
    try:
        span = max(0.0, float(window[1]) - float(window[0]))
    except (TypeError, ValueError, IndexError):
        span = 0.0
    n = math.ceil(span / secs) if span > 0 else lo
    return max(lo, min(hi, n))


def _accepts_window(fn) -> bool:
    """fn 是否接受 window 关键字参数(显式形参或 **kwargs)。用它替代"盲 except
    TypeError 再不带窗重试"——那样会把采样器/视频调用【内部】真正的 TypeError 也
    吞掉、悄悄退化成整段分析(却仍打 focus 标签)。签名探测不到 → 保守当作不支持。"""
    try:
        import inspect
        params = inspect.signature(fn).parameters
        if "window" in params:
            return True
        return any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return False


def sample_reference_frames(video_path: str, out_dir: str, count=None, window=None) -> list:
    """给现写模型抽参考静帧(纯 ffmpeg,含降采样,无 LLM 成本)。count=None → 按时长自适应;
    window=(start,end) → 只在该窗内抽,且默认用更密的"聚焦帧数"(让现写模型看清那一小段)。
    模块5 用它:即使分析走 native(整段喂视频、无帧),也让现写模型能"看到"参考画面。"""
    try:
        if count:
            n = int(count)
        elif window:
            n = _focus_frame_count(window)
        else:
            n = _adaptive_frame_count(_probe_duration(Path(video_path)))
        return _default_sampler(video_path, out_dir, n, window=window)
    except Exception:  # noqa: BLE001 —— 抽帧失败不拖垮现写
        return []


def _video_backoffs() -> list:
    """429 退避秒序列。默认 5,15,30(跨过每分钟 RPM 窗口);env 可覆盖,测试设 '0,0' 免真 sleep。"""
    raw = os.getenv("STYLEREF_VIDEO_RETRY_BACKOFF", "5,15,30")
    out = []
    for x in raw.split(","):
        try:
            out.append(max(0.0, float(x.strip())))
        except ValueError:
            continue
    return out or [5.0, 15.0, 30.0]


def _trim_clip(video_path: str, window):
    """把视频裁到 window=(start,end) 的临时 mp4(供 native 聚焦分析,帧精确 seek)。
    失败→None(上层回退整片)。调用方读完字节应立即删临时文件。"""
    try:
        ws, we = float(window[0]), float(window[1])
    except (TypeError, ValueError, IndexError):
        return None
    if we <= ws:
        return None
    import tempfile
    try:
        fd, out = tempfile.mkstemp(suffix=".mp4", prefix="styleref_focus_")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", f"{ws:.2f}", "-to", f"{we:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-an", out],
            capture_output=True, timeout=120)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        try:
            os.remove(out)
        except OSError:
            pass
        return None
    except Exception:  # noqa: BLE001
        return None


def _default_video_call(video_path: str, prompt: str, window=None) -> dict:
    """Gemini 原生视频(generateContent + inline base64)。**真到集成按第1条规矩实测端点**。
    未配置/不支持一律抛异常 → 上层降级抽帧。"""
    import requests
    # STYLEREF_GEMINI_KEY 专供 native 视频分析(可指向另一把有配额/计费的 key);
    # 未设则回退 AUTHOR_LLM_API_KEY / VISION_LLM_API_KEY(向后兼容,其它路不受影响)。
    key = (os.getenv("STYLEREF_GEMINI_KEY")
           or os.getenv("AUTHOR_LLM_API_KEY") or os.getenv("VISION_LLM_API_KEY", ""))
    model = os.getenv("STYLEREF_VIDEO_MODEL", "gemini-2.0-flash")
    if not key:
        raise RuntimeError("未配置 Gemini key(AUTHOR_LLM_API_KEY/VISION_LLM_API_KEY)")
    _tmp = _trim_clip(video_path, window) if window is not None else None
    read_from = _tmp or video_path       # window → 先裁成临时短片再喂 native(聚焦)
    try:
        b64 = base64.b64encode(Path(read_from).read_bytes()).decode()
    finally:
        if _tmp:
            try:
                os.remove(_tmp)          # 无论读成功与否都删临时片,防泄漏
            except OSError:
                pass
    # key 走请求头 x-goog-api-key(AIza/AQ. 两种格式都兼容;不进 URL = 报错不泄漏 key)。
    base = os.getenv("STYLEREF_GEMINI_BASE", "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "video/mp4", "data": b64}},
        {"text": prompt},
    ]}]}
    import time
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    delays = _video_backoffs()
    for i in range(len(delays) + 1):
        r = requests.post(url, json=body, headers=headers, timeout=240)
        if r.status_code == 429 and i < len(delays):
            logger.warning(f"原生视频限流(429),{delays[i]:.0f}s 后重试第 {i + 1} 次")
            time.sleep(delays[i])
            continue
        break
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return {"content": text, "usage": data.get("usageMetadata") or {}}


def _probe_duration(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=30)
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return 0.0


# ─────────────────────────── 分析子路 ───────────────────────────

def _analyze_image(p: Path, aspects: list, vision_call, corrections: list) -> dict | None:
    content = [{"type": "text", "text": "REFERENCE IMAGE (analyze its style):"},
               {"type": "image_url", "image_url": {"url": _b64_data_url(p)}},
               {"type": "text", "text": _prompt(aspects)}]
    try:
        raw = (vision_call or _default_vision_call)(
            [{"role": "user", "content": content}], MAX_TOKENS, TEMP)
    except Exception as e:  # noqa: BLE001
        corrections.append(f"图片分析调用失败: {type(e).__name__}: {e}")
        return None
    spec = _parse_spec(raw.get("content", ""), aspects, corrections)
    if spec is not None:
        spec["analysis_mode"] = "image"
        spec["source_frames"] = [str(p)]
        _attach_cost(spec, raw)
    return spec


def _analyze_video_native(p: Path, aspects: list, video_call, corrections: list,
                          window=None, focus_hint=None) -> dict | None:
    vfn = video_call or _default_video_call
    prompt = _prompt(aspects, focus_hint)
    try:
        if window is not None and _accepts_window(vfn):
            raw = vfn(str(p), prompt, window=window)
        else:
            if window is not None:       # 注入的 video_call 不认 window → 退回整段
                corrections.append("video_call 不支持 window,聚焦退化为整段 native")
            raw = vfn(str(p), prompt)
    except Exception as e:  # noqa: BLE001 —— 失败 → 上层降级抽帧
        corrections.append(f"原生视频分析失败,降级抽帧: {type(e).__name__}: {e}")
        return None
    spec = _parse_spec(raw.get("content", ""), aspects, corrections)
    if spec is not None:
        spec["analysis_mode"] = "video"
        spec["source_frames"] = []
        _attach_cost(spec, raw)
    return spec


def _analyze_frames(p: Path, aspects: list, out_dir, sampler, vision_call,
                    frame_count: int, corrections: list, window=None, focus_hint=None) -> dict | None:
    outd = str(out_dir or (p.parent / ".style_ref"))
    sfn = sampler or _default_sampler
    try:
        if window is not None and _accepts_window(sfn):
            frames = sfn(str(p), outd, frame_count, window=window)
        else:
            if window is not None:       # 注入的采样器不认 window → 退回整段并记因
                corrections.append("采样器不支持 window,聚焦退化为整段抽帧")
            frames = sfn(str(p), outd, frame_count)
    except Exception as e:  # noqa: BLE001
        corrections.append(f"抽帧失败: {type(e).__name__}: {e}")
        frames = []
    if not frames:
        corrections.append("没抽到帧,无法分析")
        return None
    if focus_hint:
        hdr = (f"These {len(frames)} keyframes are a SHORT SEGMENT ({focus_hint}) the user "
               "singled out (in order). Study the specific visual effect happening here:")
    else:
        hdr = (f"REFERENCE VIDEO sampled into {len(frames)} keyframes (in order). "
               "Infer editing/visual style (incl. transitions/pacing) from the sequence:")
    content = [{"type": "text", "text": hdr}]
    for i, fp in enumerate(frames):
        content.append({"type": "text", "text": f"[frame {i + 1}/{len(frames)}]"})
        content.append({"type": "image_url", "image_url": {"url": _b64_data_url(Path(fp))}})
    content.append({"type": "text", "text": _prompt(aspects, focus_hint)})
    try:
        raw = (vision_call or _default_vision_call)(
            [{"role": "user", "content": content}], MAX_TOKENS, TEMP)
    except Exception as e:  # noqa: BLE001
        corrections.append(f"帧序列分析调用失败: {type(e).__name__}: {e}")
        return None
    spec = _parse_spec(raw.get("content", ""), aspects, corrections)
    if spec is not None:
        spec["analysis_mode"] = "frames"
        spec["source_frames"] = frames
        _attach_cost(spec, raw)
    return spec


# ─────────────────────────── 入口 ───────────────────────────

def analyze_reference(ref_path, aspects, *, out_dir=None,
                      duration_s: float | None = None, size_bytes: int | None = None,
                      video_call=None, vision_call=None, sampler=None,
                      max_video_s: float | None = None, max_video_mb: float | None = None,
                      frame_count=None, window=None, focus_hint=None) -> dict:
    """参考素材 → StyleSpec。永不抛异常;任何失败 → 空 StyleSpec(带 corrections)。"""
    aspects = _clean_aspects(aspects)
    corrections: list = []
    try:
        p = Path(ref_path)
        if not p.exists():
            corrections.append(f"参考素材不存在: {ref_path}")
            return _finish(empty_style_spec(), aspects, corrections)
        ext = p.suffix.lower()
        spec = None
        if ext in IMAGE_EXTS:
            spec = _analyze_image(p, aspects, vision_call, corrections)
        elif ext in VIDEO_EXTS:
            thr_s = max_video_s if max_video_s is not None else float(os.getenv("STYLEREF_MAX_VIDEO_S", "30"))
            thr_mb = max_video_mb if max_video_mb is not None else float(os.getenv("STYLEREF_MAX_VIDEO_MB", "20"))
            dur = duration_s if duration_s is not None else _probe_duration(p)
            size_mb = (size_bytes if size_bytes is not None else p.stat().st_size) / 1e6
            if window is not None:
                # 聚焦:native 只喂裁出的窗,按【窗长】而非整片判阈值,体积按比例估
                try:
                    win_span = max(0.0, float(window[1]) - float(window[0]))
                except (TypeError, ValueError, IndexError):
                    win_span = 0.0
                est_mb = size_mb * (win_span / dur) if dur > 0 and win_span > 0 else size_mb
                within = (win_span <= thr_s if win_span > 0 else True) and (est_mb <= thr_mb)
            else:
                within = (dur <= thr_s if dur > 0 else True) and (size_mb <= thr_mb)
            # 默认走抽帧(抽帧留下的静帧能喂给现写模型,对"模仿外观"更有效)。
            # native(整段喂视频)仅在 STYLEREF_PREFER_NATIVE=1 时启用,作为"动态风格"可选分析。
            prefer_native = os.getenv("STYLEREF_PREFER_NATIVE", "0").strip().lower() in ("1", "true", "yes", "on")
            if prefer_native and within:
                spec = _analyze_video_native(p, aspects, video_call, corrections,
                                             window=window, focus_hint=focus_hint)
            elif prefer_native and not within:
                corrections.append(f"参考超阈值(时长{dur:.0f}s/体积{size_mb:.0f}MB),走抽帧")
            if spec is None:                       # 默认抽帧 / native 失败 / 超阈值 → 抽帧
                if frame_count:
                    fc = int(frame_count)
                elif window is not None:
                    fc = _focus_frame_count(window)      # 聚焦窗:更密的帧数
                else:
                    fc = _adaptive_frame_count(dur)      # 整体:按时长自适应
                spec = _analyze_frames(p, aspects, out_dir, sampler, vision_call, fc,
                                       corrections, window=window, focus_hint=focus_hint)
        else:
            corrections.append(f"不支持的参考类型: {ext}")
        if spec is None:
            return _finish(empty_style_spec(), aspects, corrections)
        return _finish(spec, aspects, corrections)
    except Exception as e:  # noqa: BLE001 —— 入口绝不抛
        corrections.append(f"未预期异常: {type(e).__name__}: {e}")
        return _finish(empty_style_spec(), aspects, corrections)


def _finish(spec: dict, aspects: list, corrections: list) -> dict:
    spec.setdefault("analysis_mode", None)
    spec.setdefault("overall", "")
    spec.setdefault("source_frames", [])
    spec.setdefault("usage", {})
    spec.setdefault("cost_usd", 0.0)
    spec["aspects"] = aspects
    spec["corrections"] = corrections + list(spec.get("corrections", []))
    return spec