"""渲染前 QA stills——video-studio 流程的机器可查部分。

video-studio 的质量来源之一是"渲染整片之前先抽帧看"（CLAUDE-v2 §3/§8：
`npx remotion still --scale=0.5` 逐 beat 抽查，比整片渲染便宜两个数量级）。
WhatsApp 自动管线里没有人眼，这个模块把清单里**机器能查的部分**自动化：

- 选帧：intro 落位后、每张数据卡完全展开后、卡片全屏区间中点、片尾——
  正是 codex 说的 "intro beat / first data display frame / section midpoints"。
- 检查：停靠模式下内容区的填充率（空画布检测——参考构建的核心规则是
  "卡片缩小必须是为了给内容让位"，缩了却没内容 = 布局 bug）。

查不了的部分（脸的位置对不对、图形跟口播语义配不配）需要有视觉能力的
agent 看 stills 决定——所以这里把 stills 路径 + 结构化 findings 一起返回，
P1 的 L2 agent 以后可以直接消费。找不到 npx / 组合没注册时整体跳过并返回
空结果，绝不让 QA 环节本身搞垮渲染。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FPS = 30
_SCALE = 0.5
_STILL_TIMEOUT_S = 120
# Fix C8：一次批处理调用的整体超时——批处理本身就是要省掉 N 次单独冷启动，
# 单帧超时(_STILL_TIMEOUT_S)乘以帧数会过于宽松，但也不能卡在原地不动；给
# 每帧留够时间的同时设一个绝对上限，超了就当批处理失败，回退到逐帧路径。
_BATCH_TIMEOUT_S = 180

# Must match pipeline_runner's geometry (single source of truth would be a
# circular import; this mirrors _DOMINANT_BOX/_workflow_box, change together).
# Workflow mode: card docks small at top-right, height fixed at 900 (P3:
# width now varies 340-300px with how much on-screen content needs, see
# pipeline_runner._workflow_box — height is what is_docked() below checks,
# and it doesn't change across content widths). The graphics (dataCards/
# gauges/countdowns/calendars/beforeAfter, default y=900, and section
# takeovers spanning most of the canvas) occupy the freed canvas — the fill
# check samples that region.
_WORKFLOW_BOX_H = 900
# Previously only a 330px/17%-of-frame-height band (y=860-1190) — a frame
# could be entirely empty everywhere else in the canvas and still pass.
# Widened to span from where content-zone graphics conventionally start
# (y=900) down to the pinned BrandBar/ComplianceBar zone (y=1824, see
# theme.ts's BRAND/COMPLIANCE constants), and nearly the full canvas width —
# now ~47% of frame height instead of ~17%. This overlaps the Workflow pip's
# own bottom-right footprint for a small sliver (y=900-1004) — the pip
# itself isn't background color, so that sliver alone can't make a truly
# empty frame register as "filled"; the remaining ~800px of the zone is
# unaffected and carries the real signal.
_CONTENT_ZONE = {"x0": 40, "y0": 900, "x1": 1040, "y1": 1800}
_BG = {"warm": (0xF2, 0xEB, 0xE0), "dark": (0x0D, 0x11, 0x17)}
_MIN_CONTENT_FILL = 0.06  # below this, a docked frame's content zone is "empty canvas"


def _card_h_at(scenes: list[dict], frame: int) -> float:
    """SpeakerCard 同款线性关键帧插值（easing 不改变端点值，对停留区间取值精确）。"""
    if not scenes:
        return 0
    prev = scenes[0]
    if frame <= prev["frame"]:
        return prev["h"]
    for s in scenes[1:]:
        if frame <= s["frame"]:
            span = s["frame"] - prev["frame"]
            t = (frame - prev["frame"]) / span if span else 1.0
            return prev["h"] + (s["h"] - prev["h"]) * t
        prev = s
    return prev["h"]


def is_docked(scenes: list[dict], frame: int) -> bool:
    return abs(_card_h_at(scenes, frame) - _WORKFLOW_BOX_H) < 1


def _transition_windows(scenes: list[dict]) -> list[tuple[int, int]]:
    """跟 props_lint._transition_windows 同一套定义(改动要两边一起改，见本
    文件顶部关于跟 pipeline_runner 几何镜像的注释)——SpeakerCard 正在两个
    scene 关键帧之间变形(w/h 改变)的帧区间。"""
    windows = []
    for i in range(len(scenes) - 1):
        a, b = scenes[i], scenes[i + 1]
        if a.get("w") != b.get("w") or a.get("h") != b.get("h"):
            windows.append((a["frame"], b["frame"]))
    return windows


def pick_qa_frames(props: dict) -> list[int]:
    """codex 的抽查点：intro 落位、每张数据卡全展开、每个前后对比卡全展开、
    每个全画布接管的中点、全屏区间中点、片尾、每次卡片转场前后各一帧。"""
    duration_frames = max(1, round(props["durationSeconds"] * _FPS))
    frames = {min(props.get("introOutFrame", 20) + 15, duration_frames - 1)}
    # Fix C46 (2026-07-21, real production reproduction, job_452ef6c48100):
    # a count_up row's number animates via `interpolate(rowLocal, [0, 40],
    # [0, row.value], ...)` in InfoCard.tsx — 40 frames from that row's own
    # mountOffset to its FINAL value. This used to sample only mountFrame +
    # last_row + 30, i.e. 10 frames before the animation settles, catching the
    # still-counting-up number and misreading it as a wrong delivered value.
    # Confirmed exactly: a still sampled at +30/40 of the way through reads
    # ~75% of the true target ($8,400 -> "$6,300", $1,500,000 -> "$1.1M" after
    # formatting) — bit-for-bit what vision QA flagged as a content mismatch
    # on two separate real runs. +45 (not just +40) leaves a small settle
    # margin past the interpolate's own clamp point.
    for card in props.get("dataCards", []):
        last_row = max((r.get("mountOffset", 0) for r in card.get("rows", [])), default=0)
        frames.add(min(card["mountFrame"] + last_row + 45, duration_frames - 1))
    # beforeAfter cards weren't sampled at all before this — both values need
    # to have actually landed, not just the card's own mount, for the still
    # to show the finished reveal.
    for card in props.get("beforeAfter", []):
        frames.add(min(card["secondRevealFrame"] + 40, duration_frames - 1))
    # Section takeovers (incl. any process timeline they carry) are commonly
    # docked the whole time they're on screen, so the "full-screen interval
    # midpoint" sampling below never catches them — sample each one directly.
    for sec in props.get("sections", []):
        mid = (sec["fromFrame"] + sec["toFrame"]) // 2
        frames.add(min(max(mid, 0), duration_frames - 1))
    frames.add(max(duration_frames - 30, 0))
    scenes = props.get("scenes", [])
    full_frames = [f for f in range(0, duration_frames, 30) if not is_docked(scenes, f)]
    if full_frames:
        frames.add(full_frames[len(full_frames) // 2])
    # Fix C7：CLAUDE-v2.md §9 "Transitions"标准要求的抽查方式——每次转场取
    # 前后各一帧(f[start-8]/f[end+8])，"outgoing cluster fully gone in the
    # SECOND still, not fading in the first"这条只有视觉能判断，之前完全没
    # 采样过转场前后的帧对，视觉复审拿到的都是转场以外的帧，没法评价转场
    # 干不干净。
    for win_start, win_end in _transition_windows(scenes):
        frames.add(max(0, min(win_start - 8, duration_frames - 1)))
        frames.add(max(0, min(win_end + 8, duration_frames - 1)))
    # Fix C48（2026-07-21，同一支 job_452ef6c48100，就在验证 C46 的下一次
    # 真实渲染里复现——这次是一个不同的 finding："Annual Premium $4,200"
    # 对着转写的 "$8,400"，4200 恰好是 8400 的 50%，也就是 interpolate(
    # rowLocal, [0, 40], ...) 走到第 20 帧（40 的一半）时的读数，不是 C46
    # 修过的那条数据卡专属采样规则算出来的帧（那条规则现在直接跳到 +45，
    # 已经在窗口结束之后）——是上面 transition-window/full-frame 等其它规则
    # 独立算出的某个帧，恰好也落进了同一个 count_up 行还在数数的窗口内。
    # C46 只修了"数据卡专属"这一条规则，没有堵住其它规则各自算出的帧同样
    # 可能落进同一个动画窗口——重演的正是 Fix C22 自己写下的教训："不要
    # 特事特办每条可能算出问题帧的规则，在唯一真正重要的地方强制这条不
    # 变量"。所以这里不再头痛医头，改成在真正返回之前统一扫一遍：任何
    # 规则算出的帧，只要落进了任意一个 count_up 行自己的动画窗口
    # [mountFrame+mountOffset, +45)，一律推到那个窗口结束之后，不管是哪条
    # 规则产生的。
    count_up_windows: list[tuple[int, int]] = []
    for card in props.get("dataCards", []) or []:
        mount = card.get("mountFrame")
        if mount is None:
            continue
        for row in card.get("rows", []) or []:
            if not isinstance(row, dict) or row.get("value") is None:
                continue
            start = mount + row.get("mountOffset", 0)
            count_up_windows.append((start, min(start + 45, duration_frames)))
    if count_up_windows:
        adjusted = set()
        for f in frames:
            shifted = f
            moved = True
            while moved:
                moved = False
                for start, end in count_up_windows:
                    if start <= shifted < end:
                        shifted = min(end, duration_frames - 1)
                        moved = True
            adjusted.add(shifted)
        frames = adjusted

    # Fix C22（2026-07-19，真实生产复现 job_ac00838adea9/job_1b7254abcd66，
    # both times reproduced again on a live re-run after the first attempt at
    # this fix only patched the transition-window path above）: frame 0 is the
    # instant before the SpeakerCard's entrance animation has rendered
    # anything — a genuinely blank canvas, confirmed by opening f0.png
    # directly. It gets sampled through *multiple* independent rules, not just
    # one: scenes[0] is always {"frame": 0, ...}, so the transition-window
    # loop above always samples max(0, 0-8)=0; separately, whenever the
    # opening Dominant/full-size hold is brief enough that frame 0 is the only
    # (or a low-index) entry in `full_frames`, the "full-screen interval
    # midpoint" rule two blocks up *also* resolves to 0 (confirmed live: a
    # re-run of job_ac00838adea9 with a regenerated 20-frame-long intro hold
    # produced full_frames == [0], reintroducing frame 0 through this
    # completely different rule after the transition-window path alone had
    # already been patched). Content_planner has zero control over the
    # SpeakerCard's own entrance timing, so no amount of replanning can ever
    # change what this frame looks like — every retry regenerates the same
    # emptiness, and vision QA's severity call on it is not deterministic
    # (`job_localdemowalk` scored the same essential frame "无内容/low" and
    # shipped fine; both jobs above scored it "空画布/high" and degraded).
    # Rather than special-case every individual rule that might independently
    # land on 0 (proven fragile — that's exactly how the first attempt at this
    # fix missed the full_frames path), enforce the invariant once, here, at
    # the only point that actually matters: no rule above needs frame 0
    # specifically (the "intro landed" check already samples
    # introOutFrame+15, well after the entrance), so it is never a frame worth
    # sending to vision QA regardless of which rule produced it.
    return sorted(f for f in frames if 0 < f < duration_frames)


def render_still(remotion_dir: Path, props_path: Path, frame: int, out_png: Path) -> bool:
    # Windows: subprocess needs the resolved npx.cmd, plain "npx" raises
    # WinError 2 (same fix already applied to the real render call in
    # pipeline_runner.py's _op_apply_style — this one was missed, silently
    # disabling QA stills AND the vision-review step that depends on them
    # on any Windows deployment).
    npx_bin = shutil.which("npx") or "npx"
    from .remotion_bundle import ensure_remotion_bundle
    bundle = ensure_remotion_bundle(Path(remotion_dir))
    # props_path/out_png must be absolute — this subprocess runs with
    # cwd=remotion_dir, so a relative path (e.g. "storage/jobs/<id>/...json")
    # resolves against remotion-composer/ instead of the repo root, and
    # Remotion rejects it outright. Confirmed real production bug: this was
    # the actual cause of every "still fN 渲染失败" — not a render flake.
    cmd = [npx_bin, "remotion", "still"] + ([bundle] if bundle else []) + [
        "XiaojinEditorial", str(out_png.resolve()),
        f"--frame={frame}", f"--props={props_path.resolve()}", f"--scale={_SCALE}",
    ]
    try:
        # 每张 still 都是一次 headless Chrome 渲染——必须和整片渲染共用同一个
        # 闸门，否则两个任务的 QA（视觉重试后各 8-12 张）并跑照样互踩。
        from .concurrency import RENDER_SLOTS
        with RENDER_SLOTS:
            r = subprocess.run(cmd, cwd=remotion_dir, capture_output=True, text=True, timeout=_STILL_TIMEOUT_S)
    except Exception as e:
        logger.warning(f"  qa_stills: still f{frame} 渲染异常: {e}")
        return False
    if r.returncode != 0:
        logger.warning(f"  qa_stills: still f{frame} 渲染失败: {(r.stderr or '').strip()[-300:]}")
        return False
    return out_png.exists()


def render_stills_batch(
    remotion_dir: Path, props_path: Path, frames: list[int], out_dir: Path
) -> Optional[dict[int, Path]]:
    """Fix C8（2026-07-16）：一次 Node 进程渲染全部 stills，而不是每帧一次
    `npx remotion still`（每次都要重新起 Node+Chrome）。跟 CLAUDE-v2.md §11
    记录的 video-studio 同款优化同一个原理——bundle 一次、Chrome 开一次，
    这个仓库自己在 remotion-composer/scripts/batch-stills.mjs 里已经有一份
    移植好但从未真正接进管线的实现（只是没人传 inputProps，也没接到这里）。
    成功返回 {frame: png_path}；任何一步失败返回 None，调用方回退到
    render_still() 逐帧路径——批处理只是加速器，不是新的失败模式。"""
    script = remotion_dir / "scripts" / "batch-stills.mjs"
    if not script.exists():
        return None
    node_bin = shutil.which("node") or "node"  # same WinError 2 guard as npx elsewhere in this module
    from .remotion_bundle import ensure_remotion_bundle
    bundle = ensure_remotion_bundle(Path(remotion_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_arg = ",".join(str(f) for f in frames)
    cmd = [node_bin, str(script.resolve()), "XiaojinEditorial", str(out_dir.resolve()),
           frames_arg, str(_SCALE), str(props_path.resolve())]
    if bundle:
        cmd.append(bundle)
    try:
        from .concurrency import RENDER_SLOTS
        with RENDER_SLOTS:
            r = subprocess.run(cmd, cwd=remotion_dir, capture_output=True, text=True, timeout=_BATCH_TIMEOUT_S)
    except Exception as e:
        logger.warning(f"  qa_stills: 批量渲染异常，回退逐帧: {e}")
        return None
    if r.returncode != 0:
        logger.warning(f"  qa_stills: 批量渲染失败，回退逐帧: {(r.stderr or '').strip()[-300:]}")
        return None
    result: dict[int, Path] = {}
    for f in frames:
        png = out_dir / f"f{f}.png"
        if png.exists():
            result[f] = png
    if len(result) != len(frames):
        logger.warning(
            f"  qa_stills: 批量渲染只产出 {len(result)}/{len(frames)} 张，回退逐帧补齐缺失的"
        )
        return None
    return result


def check_content_fill(png_path: Path, props: dict, frame: int) -> Optional[dict]:
    """停靠帧的内容区填充率检查。返回 finding dict 或 None（通过/不适用）。"""
    scenes = props.get("scenes", [])
    if not is_docked(scenes, frame):
        return None  # full-screen frame — the card itself fills the canvas
    try:
        from PIL import Image
    except ImportError:
        return None

    bg = _BG.get(props.get("colorMode", "warm"), _BG["warm"])
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        zone = im.crop((
            round(_CONTENT_ZONE["x0"] * _SCALE), round(_CONTENT_ZONE["y0"] * _SCALE),
            round(_CONTENT_ZONE["x1"] * _SCALE), round(_CONTENT_ZONE["y1"] * _SCALE),
        ))
        px = list(zone.getdata())
    if not px:
        return None
    non_bg = sum(1 for (r, g, b) in px if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 45)
    fill = non_bg / len(px)
    if fill < _MIN_CONTENT_FILL:
        return {
            "check": "empty_canvas",
            "frame": frame,
            "fill_ratio": round(fill, 4),
            "detail": "卡片处于停靠(缩小)状态但内容区基本是空的——缩小必须是为了给内容让位",
        }
    return None


def run_props_qa(props: dict, props_path: Path, remotion_dir: Path, out_dir: Path) -> dict:
    """渲 QA stills + 跑机器检查。永不 raise；渲染环境不可用时返回空结果。

    返回 {"stills": [{"frame", "path"}...], "findings": [finding...]}——
    stills 留在 out_dir 里，供有视觉的 agent（P1 的 L2 线）后续人工级审查。
    """
    result: dict = {"stills": [], "findings": []}
    if not (remotion_dir / "package.json").exists():
        logger.info("  qa_stills: remotion-composer 不可用，跳过 QA stills")
        return result
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = pick_qa_frames(props)
    # Fix C8：先试批处理（一次 bundle + 一次 Chrome，见 render_stills_batch）；
    # 失败（脚本不存在/超时/产出不全）就回退到原来逐帧 render_still 的路径，
    # 批处理只是加速器，两条路径最终产出同一种 {frame: png} 形状，下游检查
    # 逻辑不用关心走的是哪一条。
    batch_pngs = render_stills_batch(remotion_dir, props_path, frames, out_dir)
    for frame in frames:
        if batch_pngs is not None:
            png = batch_pngs.get(frame)
            ok = png is not None
        else:
            png = out_dir / f"qa_f{frame}.png"
            ok = render_still(remotion_dir, props_path, frame, png)
        if not ok:
            result["findings"].append({"check": "still_render_failed", "frame": frame})
            continue
        result["stills"].append({"frame": frame, "path": str(png)})
        finding = check_content_fill(png, props, frame)
        if finding:
            result["findings"].append(finding)

    # 视觉复审（video-studio CLAUDE-v2 §9 清单里"机器查不了、需要眼睛"的部分）：
    # 把 stills 交给视觉子模型（VISION_LLM_*，如 GLM-4V）对照清单挑毛病。
    # DeepSeek 主通道是纯文本模型看不了图，所以这一步走独立的视觉通道；
    # 未配置或调用失败都只是"没有眼睛"，绝不影响渲染。
    vision = _vision_review_confirmed([s["path"] for s in result["stills"]])
    if vision:
        result["vision_review"] = vision
        for f in vision.get("findings", []):
            logger.warning(f"  qa_stills 视觉复审发现: {f}")

    (out_dir / "qa_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for f in result["findings"]:
        logger.warning(f"  qa_stills 发现问题: {f}")
    return result


_VISION_CHECKLIST = """这些是同一条竖屏(1080x1920)成片视频在不同时间点的抽帧。请对照以下清单逐帧检查，只报告确实存在的问题：
1. 说话人取景：脸是否被裁切/贴边？脸应在其卡片顶部 20-40% 位置，胸肩可见。
2. 元素重叠：字幕、数据卡、图标、章节导航之间是否互相遮挡？
3. 空画布：说话人卡片缩小时，腾出的画面是否大面积空白（没有任何内容填充）？
4. 文字问题：是否有文字被截断、溢出容器、或小到不可读？
5. 对比度：文字/图形与背景颜色是否难以分辨？
6. 转场是否干净（Fix C7，CLAUDE-v2.md §9 "Transitions"）：部分图片是紧挨着卡片转场
   前后各取的一对（按拍摄顺序相邻的两张，索引连续）——后一张里，前一张还在场的元素
   应该已经完全消失，不能是"消失了一半"还叠在正在移动/变形的卡片上。
7. 是否符合品牌视觉识别（Fix C7，CLAUDE-v2.md §9 "Reference match"）：说话人应该
   在一张浮动圆角卡片里，绝不铺满全屏（除非是全画布接管场景）；背景应该是暖米色
   或深色两者之一，不应该是纯黑/纯白；应该能看到顶部章节导航条和底部彩虹进度条。
8. 视觉是否统一连贯（Fix C7，CLAUDE-v2.md §9 "Visual cohesion"）：所有抽帧里的
   字体、卡片圆角、阴影深浅是否一致？有没有哪一帧的配色/风格明显跳脱，像是另一条
   视频混进来的？
9. 图形是否配合口播内容（Fix C7，CLAUDE-v2.md §9 "Beat-to-caption sync"）：每张
   图里如果烧录了字幕文字，读一下字幕说的是什么，再看当前画面上的图形/数据卡内容
   是否真的跟这句话有关——不要求精确对应，但明显文不对题（比如字幕在说完全不相关
   的内容，画面上却是上一个话题的图表还没撤）算一个问题。

输出 JSON（只输出 JSON）：{"findings": [{"frame_index": 第几张图(从0起), "issue": "一句话描述", "severity": "high|low"}], "overall": "一句话总评"}
没有问题就输出 {"findings": [], "overall": "..."}。不要为了凑数报告不存在的问题。"""


def _vision_review(still_paths: list) -> Optional[dict]:
    """视觉子模型复审 stills。返回 {"findings": [...], "overall": str} 或 None。"""
    if not still_paths:
        return None
    try:
        from .llm_client import call_vision_chat

        raw = call_vision_chat(_VISION_CHECKLIST, still_paths[:5])
        if not raw:
            return None
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]
        data = json.loads(cleaned)
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return data
        return None
    except Exception as e:
        logger.warning(f"  qa_stills: 视觉复审异常（跳过）: {e}")
        return None


def _vision_review_confirmed(still_paths: list) -> Optional[dict]:
    """Fix C23（2026-07-20，真实复现 job_452ef6c48100，用户反馈"自从接了视觉
    LLM 之后一直这样"促成的排查）：VISION_LLM_MODEL=glm-4v-flash 对同一帧的
    判断不是确定性的——这不是新发现，Rule 14 已经记录过它对同一张帧在不同
    job 里分别判成"无内容/low"和"空画布/high"。这次直接打开被标记为 high
    的实际截图核实：frame_index 0 被报"'POLICY RENEWAL REMINDER' 被截断"和
    "'David from Pacific Life' 被截断"——两段文字在图上都完整可读，根本没有
    截断；"脸部被裁切"是否属实还存疑（取景确实偏紧）但另外两条是纯粹的
    模型幻觉。单次 high 判断就直接喂回重规划、重试后仍 high 就整段降级交付
    给用户——一次不可靠模型调用的噪音，被这条链路放大成用户能看到的失败。

    这里不改变"发现了就重规划、重规划完还不行就降级"的既有流程（那套本身
    是对的），只是在把某条 finding 当作"真的存在"之前加一道确认：对同一批
    已经渲染好的 stills（不需要重新渲染，只多一次纯视觉 LLM 调用）再问一遍，
    只有两次调用都判定同一 frame_index 是 high 严重度问题时才采信——真实存在
    的缺陷（取景/重叠/空画布这类客观视觉事实）大概率会在第二次调用里复现，
    单次模型噪音大概率不会精确复现在同一帧上。low 严重度发现仅供参考、不
    驱动任何重试/降级决策，不需要确认。

    只在第一次调用真的出现 high 发现时才多花这一次确认调用——没有发现的
    "干净"路径（多数情况）成本不变。
    """
    first = _vision_review(still_paths)
    if not first:
        return first
    first_high = {f["frame_index"] for f in first.get("findings", [])
                  if f.get("severity") == "high" and "frame_index" in f}
    if not first_high:
        return first

    second = _vision_review(still_paths)
    if not second:
        # 二次确认调用本身失败（网络/未配置）——宁可保守地当作未确认，不让
        # 一条从没被复核过的 high 发现单独触发重规划/降级。
        logger.warning("  qa_stills: 二次视觉复核调用失败，无法确认高严重度发现，按未发现处理")
        kept = [f for f in first.get("findings", []) if f.get("severity") != "high"]
        return {"findings": kept, "overall": first.get("overall", "")}

    second_high = {f["frame_index"] for f in second.get("findings", [])
                   if f.get("severity") == "high" and "frame_index" in f}
    reproduced = first_high & second_high
    dropped = first_high - reproduced
    if dropped:
        logger.info(
            f"  qa_stills: 高严重度发现二次复核未复现，判定为模型噪音丢弃"
            f"（frame_index: {sorted(dropped)}）"
        )

    kept = [
        f for f in first.get("findings", [])
        if f.get("severity") != "high" or f.get("frame_index") in reproduced
    ]
    return {"findings": kept, "overall": first.get("overall", "")}
