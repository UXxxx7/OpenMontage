"""L2 原型（AGENT_GUIDE 治理完整版）：agent 工具调用循环，复用 OpenMontage handler。

对齐 AGENT_GUIDE.md：
- Rule Zero：读 manifest + edit-director skill + 各工具的 Layer 3 skill 进上下文；
- 工具白名单从 manifest 的 tools_available 动态读（只暴露 pipeline 允许的工具）；
- Reviewer 协议：规划后自审(load review_focus)，critical 自动修订，最多 2 轮；
- Human Checkpoint：approve / 取消 / 提意见修订 三支；
- 产出 edit_decisions artifact 并按 schemas/artifacts/edit_decisions.schema.json 校验。

独立脚本，不接 WhatsApp。
用法：
    uv run python -m whatsapp_mvp.agent_editor <video.mp4> "把这条剪成适合抖音发布的"
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import requests

from . import pipeline_runner as pr
from .config import get_config

ROOT = Path(get_config().openmontage_root)

# op -> 依赖的 OpenMontage 工具（用于对照 manifest 白名单）
OP_TOOLS = {
    "trim_start": ["video_trimmer"],
    "trim_end": ["video_trimmer"],
    "keep_range": ["video_trimmer"],
    "remove_segment": ["video_trimmer"],
    "remove_silences": ["silence_cutter"],
    "speed_up_silence": ["silence_cutter"],
    "trim_leading_silence": ["silence_cutter", "video_trimmer"],
    "reframe": ["auto_reframe"],
    "add_subtitles": ["transcriber", "remotion_caption_burn"],
    "remove_filler": ["transcriber", "video_trimmer"],
    "apply_style": ["transcriber", "remotion_caption_burn"],
}

# 工具 -> Layer 3 skill（与各工具 .agent_skills 一致）
TOOL_SKILLS = {
    "silence_cutter": ["ffmpeg"],
    "video_trimmer": ["ffmpeg", "video-toolkit"],
    "auto_reframe": ["ffmpeg"],
    "transcriber": ["speech-to-text"],
    "remotion_caption_burn": ["remotion-best-practices", "ffmpeg"],
}

ALL_TOOL_SCHEMAS = {
    "trim_start": ("删掉开头 N 秒", {"seconds": {"type": "number"}}, ["seconds"]),
    "trim_end": ("删掉结尾 N 秒", {"seconds": {"type": "number"}}, ["seconds"]),
    "keep_range": ("只保留 [start, end] 这一段",
                   {"start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}},
                   ["start_seconds", "end_seconds"]),
    "remove_segment": ("删掉中间 [start, end]，保留其余",
                       {"start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}},
                       ["start_seconds", "end_seconds"]),
    "remove_silences": ("去掉静音/停顿使视频更紧凑。默认剪掉 ≥0.35s 的所有停顿；"
                        "若用户想保留自然的句间停顿、只剪明显偏长的静音，"
                        "传 min_silence_duration（秒，如 1.0~1.5）只剪超过该长度的静音",
                        {"min_silence_duration": {"type": "number",
                         "description": "只剪掉长度超过该秒数的静音；默认 0.35 几乎剪掉所有停顿，调大以保留自然停顿"}},
                        []),
    "speed_up_silence": ("把静音段加速而非删除", {"factor": {"type": "number"}}, []),
    "trim_leading_silence": ("只去掉开头的静音/空白", {}, []),
    "reframe": ("转换画幅：portrait=竖屏9:16, square=1:1, landscape=16:9, cinematic=21:9",
                {"aspect": {"type": "string", "enum": ["portrait", "square", "landscape", "cinematic"]}}, []),
    "add_subtitles": ("转写并烧录字幕（原语言）", {"language": {"type": "string"}}, []),
    "remove_filler": ("LLM 读转写判断口误/语气词/重录并剪掉；比 remove_silences 更细，"
                      "能剪有声的“呃/嗯”和中间没停顿的重录（无参）", {}, []),
    "apply_style": ("套用品牌模板出片（浮动卡片+章节+卡拉OK字幕+品牌/合规条+进度条）。"
                    "自带转写并烧字幕，不能和 add_subtitles 同时用",
                    {"template": {"type": "string", "enum": ["xiaojin-editorial"]},
                     "colorMode": {"type": "string", "enum": ["warm", "dark"]}},
                    []),
}


# ---------------------------------------------------------------------------
# 治理：读 manifest / 白名单 / review_focus / Layer 3 skill
# ---------------------------------------------------------------------------

def load_manifest() -> dict | None:
    """加载 talking-head manifest —— 直接用 OpenMontage 自带的 lib.pipeline_loader，
    它会顺带按 pipeline_manifest.schema.json 校验(比我们自己 yaml.safe_load 多一层治理)。
    lib 不可用/校验失败时优雅返回 None，agent 退回"不限制"。"""
    try:
        from lib.pipeline_loader import load_pipeline
        return load_pipeline("talking-head", defs_dir=ROOT / "pipeline_defs")
    except Exception:
        return None


def allowed_tools_from_manifest(manifest: dict | None) -> set[str]:
    if not manifest:
        return set(sum(OP_TOOLS.values(), []))  # 无 manifest 时不限制
    # lib.get_required_tools 聚合 tools_available + preferred/fallback + sub_stages
    # + reference 分析工具，比我们原来的更全。
    from lib.pipeline_loader import get_required_tools
    return get_required_tools(manifest)


def allowed_ops(allowed_tools: set[str]) -> list[str]:
    return [op for op, need in OP_TOOLS.items() if all(t in allowed_tools for t in need)]


def review_focus_for(manifest: dict | None, stage_name: str) -> list[str]:
    if not manifest:
        return []
    from lib.pipeline_loader import get_stage_review_focus
    return get_stage_review_focus(manifest, stage_name)


def _excerpt(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except Exception:
        return ""


def load_pipeline_context(ops: list[str]) -> str:
    parts: list[str] = []

    md = ROOT / "pipeline_defs" / "talking-head.yaml"
    if md.exists():
        parts.append("### Pipeline manifest（talking-head.yaml）\n```yaml\n" + _excerpt(md, 5000) + "\n```")

    ed = ROOT / "skills" / "pipelines" / "talking-head" / "edit-director.md"
    if ed.exists():
        parts.append("### Edit stage director skill（edit-director.md）\n" + _excerpt(ed, 4500))

    # Layer 3：本次可用工具引用的技术 skill（Rule Zero：用工具前读 Layer3）
    skills_needed: list[str] = []
    for op in ops:
        for tool in OP_TOOLS.get(op, []):
            for sk in TOOL_SKILLS.get(tool, []):
                if sk not in skills_needed:
                    skills_needed.append(sk)
    l3_parts = []
    for sk in skills_needed:
        for cand in (ROOT / ".agents" / "skills" / sk / "SKILL.md",):
            if cand.exists():
                l3_parts.append(f"#### Layer3: {sk}\n" + _excerpt(cand, 700))
                break
    if l3_parts:
        parts.append("### Layer 3 技术 skill（工具背后的技术要点）\n" + "\n\n".join(l3_parts))

    return "\n\n".join(parts) if parts else "（未找到 manifest/skill，按通用规范规划。）"


# ---------------------------------------------------------------------------
# function-calling schema（按白名单动态生成）
# ---------------------------------------------------------------------------

def build_tools(ops: list[str]) -> list[dict]:
    out = []
    for op in ops:
        desc, props, req = ALL_TOOL_SCHEMAS[op]
        out.append({"type": "function", "function": {
            "name": op, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}})
    out.append({"type": "function", "function": {
        "name": "finish", "description": "编辑规划完成。rationale 里简述剪辑决策依据。",
        "parameters": {"type": "object", "properties": {"rationale": {"type": "string"}}}}})
    return out


SYSTEM_BASE = """你是 OpenMontage 的剪辑 agent，编辑用户上传的 talking-head 视频。

治理要求（AGENT_GUIDE.md）：
- Rule Zero：先读下面给出的 pipeline manifest、edit-director skill、Layer3 技术 skill，按其规范规划。
- 只能用**给你的工具列表**里的工具（已按 manifest 的 tools_available 过滤）；用户要的、工具做不到的（翻译字幕、配乐、调色、换背景等），不要硬凑，在 finish 的 rationale 里点出做不了。
- 字幕(add_subtitles)若用，放最后。
- 关于停顿：用户若说"太紧/不自然/保留正常停顿"之类，不要直接放弃去静音，而是给 remove_silences 传更大的 min_silence_duration（如 1.0~1.5 秒），只剪明显偏长的静音、保留自然的句间停顿。
- 剪口误/语气词/重录：优先用 remove_filler（它读转写逐词判断，能剪有声的"呃/嗯"和无停顿的重录），不要用 remove_segment 去手动框口误。remove_silences 只删静音停顿，和 remove_filler 可叠加。
- 基于内容的"选择性剪辑"：若提供了逐句转录（带时间戳），当用户要"只保留讲 X 的部分""删掉聊 Y 那段"这类**按内容选择**的诉求，用 remove_segment(start_seconds, end_seconds) 按转录里的真实时间戳精确剪除，可多次调用。没有明确要求就不要擅自删改说话内容。
- 出片风格：用户说"剪好看点/精致/小红书风格/我们的品牌风格/做正式些"这类诉求，用 apply_style 套品牌模板出片。它**自带字幕**，选了它就不要再加 add_subtitles。
- 零指令默认：用户只说"帮我剪一下/剪一下/edit this"这类**没有任何具体指令**的请求，不要问澄清，直接默认先 remove_filler 再 apply_style，并在 finish 的 rationale 里说明这是默认处理、下次可给更具体要求。

现在是"规划阶段"：工具是**模拟执行**（只返回预计时长，不真剪）。按合理顺序调用工具，用给定时长推算模糊位置。规划完调用 finish 并在 rationale 说明依据。"""


# ---------------------------------------------------------------------------
# 模拟执行 / LLM 调用
# ---------------------------------------------------------------------------

def _simulate(name, args, duration):
    d = duration
    if name in ("trim_start", "trim_end"):
        d = max(0.0, duration - float(args.get("seconds", 0)))
    elif name == "remove_segment":
        d = max(0.0, duration - (float(args.get("end_seconds", 0)) - float(args.get("start_seconds", 0))))
    elif name == "keep_range":
        d = max(0.0, float(args.get("end_seconds", duration)) - float(args.get("start_seconds", 0)))
    elif name == "remove_silences":
        min_dur = float(args.get("min_silence_duration", 0.35) or 0.35)
        # 阈值越大剪得越少（保留自然停顿）
        factor = 0.85 if min_dur <= 0.5 else (0.92 if min_dur <= 1.0 else 0.96)
        d = round(duration * factor, 1)
    elif name == "speed_up_silence":
        d = round(duration * 0.9, 1)
    elif name == "trim_leading_silence":
        d = max(0.0, duration - 1.0)
    elif name == "remove_filler":
        d = round(duration * 0.92, 1)  # 剪口误/重录，通常比去静音删得少
    elif name == "apply_style":
        d = duration  # 只 restyle，时长不变
    return {"ok": True, "estimated_duration_seconds": round(d, 1)}, d


def _chat(config, messages, tools=None, json_mode=False):
    endpoint = (config.llm_base_url or "").rstrip("/")
    if not endpoint:
        raise RuntimeError("需要配置 LLM_BASE_URL（中转站）")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    endpoint += "/chat/completions"
    payload = {"model": config.llm_model, "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # [P1 复核] 429 退避重试（传输层容错，非规划逻辑）：agent 的 tool-calling
    # 循环一次规划要打多发请求，免费档 LLM（Gemini free tier 5 RPM）必然间歇
    # 429——等窗口重置继续，比整条规划失败回退 L1.5 兜底强得多。
    import time as _time

    for attempt in range(4):
        resp = requests.post(endpoint, headers={
            "Authorization": f"Bearer {config.llm_api_key}", "Content-Type": "application/json"},
            json=payload, timeout=90)
        if resp.status_code == 429 and attempt < 3:
            wait = 25 * (attempt + 1)
            import logging as _logging
            _logging.getLogger(__name__).warning(f"L2 LLM 429，{wait}s 后重试")
            _time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def _format_transcript(transcript, limit_chars=4000):
    """把转录段落压成 [start-end s] text 的紧凑块，超长截断。"""
    if not transcript:
        return ""
    lines = []
    for s in transcript:
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        st = float(s.get("start", 0) or 0)
        en = float(s.get("end", 0) or 0)
        lines.append(f"[{st:.1f}-{en:.1f}s] {txt}")
    block = "\n".join(lines)
    if len(block) > limit_chars:
        block = block[:limit_chars] + "\n…（转录过长已截断）"
    return block


def build_messages(request, duration, skill_context, history, supported, transcript=None, source_facts=""):
    msgs = [{"role": "system", "content": SYSTEM_BASE + "\n\n" + skill_context}]
    msgs.append({"role": "user", "content": f"视频时长 {duration:.1f} 秒。可用操作：{', '.join(supported)}。用户需求：{request}"})
    if source_facts:
        msgs.append({"role": "user", "content":
            "素材审查(source_media_review)结论,规划时据此判断"
            "(如已是竖屏就别再 reframe、有质量风险要留意):\n" + source_facts})
    tblock = _format_transcript(transcript)
    if tblock:
        msgs.append({"role": "user", "content":
            "这段视频的逐句转录（带时间戳）如下。仅当用户要求剪掉重复句/口误/自我打断/废话时，"
            "才据此用 remove_segment 精确剪除对应片段（用真实时间戳，可多次）。\n\n转录：\n" + tblock})
    for prev_plan, prev_note, feedback in history:
        summary = "；".join(
            f"{op['type']}(" + ", ".join(f"{k}={v}" for k, v in op.items() if k != "type") + ")"
            for op in prev_plan) or "（无操作）"
        msgs.append({"role": "assistant", "content": f"上一版方案：{summary}。{prev_note or ''}"})
        msgs.append({"role": "user", "content": f"请根据以下意见，重新给出**完整**方案：{feedback}"})
    return msgs


def plan_once(config, messages, tools, supported, duration, max_steps=8):
    work = list(messages)
    planned, cur, note = [], duration, ""
    for _ in range(max_steps):
        data = _chat(config, work, tools=tools)
        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls")
        if not tcs:
            note = msg.get("content") or ""
            break
        work.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        finished = False
        for tc in tcs:
            fname = tc["function"]["name"]
            try:
                fargs = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                fargs = {}
            if fname == "finish":
                finished = True
                note = fargs.get("rationale", "") or note
                sim = {"ok": True, "message": "规划结束"}
            elif fname in supported:
                planned.append({"type": fname, **fargs})
                sim, cur = _simulate(fname, fargs, cur)
            else:
                sim = {"ok": False, "error": f"工具 {fname} 不在白名单"}
            work.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(sim, ensure_ascii=False)})
        if finished:
            break
    return planned, note


# ---------------------------------------------------------------------------
# Reviewer 协议（meta/reviewer）：自审 + critical 自动修
# ---------------------------------------------------------------------------

def review_plan(config, request, plan, note, review_focus):
    focus = "\n".join(f"- {f}" for f in review_focus) or "- 方案是否达成用户需求"
    sys = ("你是 OpenMontage 的 reviewer。审查剪辑方案。只输出 JSON："
           '{"findings":[{"severity":"critical|suggestion|nitpick","note":"..."}],"verdict":"pass|revise"}。'
           "critical 仅用于：方案明显没达成用户需求、或会导致执行失败。")
    user = (f"用户需求：{request}\n方案：{json.dumps(plan, ensure_ascii=False)}\n说明：{note}\n\n"
            f"review_focus：\n{focus}\n\n给出审查 JSON。")
    try:
        data = _chat(config, [{"role": "system", "content": sys}, {"role": "user", "content": user}], json_mode=True)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(re.search(r"\{[\s\S]*\}", content).group(0))
        return parsed.get("findings", []), parsed.get("verdict", "pass")
    except Exception:
        return [], "pass"


# ---------------------------------------------------------------------------
# edit_decisions artifact + schema 校验
# ---------------------------------------------------------------------------

def build_edit_decisions(plan, video, duration, note):
    start, end = 0.0, float(duration)
    for op in plan:
        t = op.get("type")
        if t == "trim_start":
            start = max(start, float(op.get("seconds", 0)))
        elif t == "trim_end":
            end = min(end, duration - float(op.get("seconds", 0)))
        elif t == "keep_range":
            start = float(op.get("start_seconds", 0))
            end = float(op.get("end_seconds", duration))
    art = {
        "version": "1.0",
        "render_runtime": "ffmpeg",
        "cuts": [{
            "id": "primary", "source": str(video),
            "in_seconds": round(max(0.0, start), 2),
            "out_seconds": round(max(start, end), 2),
            "layer": "primary",
            "reason": (note[:200] or "primary talking-head timeline"),
        }],
        "metadata": {"operations": plan, "rationale": note},
    }
    if any(op.get("type") == "add_subtitles" for op in plan):
        art["subtitles"] = {"enabled": True, "style": "word-by-word",
                            "position": "bottom-center", "source": "transcript"}
    return art


def validate_edit_decisions(art):
    schema_path = ROOT / "schemas" / "artifacts" / "edit_decisions.schema.json"
    try:
        import jsonschema
    except ImportError:
        return None, "jsonschema 未安装，跳过校验"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(art, schema)
        return True, None
    except Exception as e:
        return False, str(e)[:300]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def execute_plan(video, plan, art):
    workdir = video.parent / "_agent_out"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "edit_decisions.json").write_text(
        json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")

    src = str(video)
    subtitle_op = None
    for op in plan:
        t = op.get("type", "")
        if t == "add_subtitles":
            subtitle_op = op
            continue
        handler = pr._OP_HANDLERS.get(t)
        if handler is None:
            print(f"  跳过不支持的操作: {t}")
            continue
        print(f"  执行 {t} {({k: v for k, v in op.items() if k != 'type'})}")
        new_src = handler(src, op, workdir)
        if new_src and Path(new_src).exists():
            src = str(new_src)
    if subtitle_op is not None:
        print("  执行 add_subtitles")
        new_src = pr._op_add_subtitles(src, subtitle_op, workdir)
        if new_src and Path(new_src).exists():
            src = str(new_src)
    out = workdir / "agent_output.mp4"
    shutil.copyfile(src, out)
    return out


# ---------------------------------------------------------------------------
# 可复用规划入口（供 WhatsApp worker 调用）：规划 + 自审 + 产 artifact
# ---------------------------------------------------------------------------

def plan_video(request: str, video_path: str | None = None,
               duration: float | None = None, history: list | None = None,
               transcript: list | None = None, source_facts: str = "") -> dict:
    """L2 规划：读 manifest/skill → agent tool-calling → reviewer 自审(critical 自动修)。

    返回与 L1.5 兼容的计划：{edit_operations, summary, edit_decisions, review_findings}。
    history 可传入 (上一版方案, 说明, 用户意见) 列表以支持就地修订。
    """
    config = get_config()
    if duration is None:
        duration = pr._probe_duration(Path(video_path)) if video_path else 0.0

    manifest = load_manifest()
    allowed = allowed_tools_from_manifest(manifest)
    ops = allowed_ops(allowed)
    rfocus = review_focus_for(manifest, "edit")
    skill_context = load_pipeline_context(ops)
    tools = build_tools(ops)
    supported = set(ops)

    work_history = list(history or [])
    messages = build_messages(request, duration, skill_context, work_history, ops)
    plan, note = plan_once(config, messages, tools, supported, duration)

    findings: list = []
    for _ in range(2):
        findings, verdict = review_plan(config, request, plan, note, rfocus)
        crit = [f for f in findings if f.get("severity") == "critical"]
        if not crit:
            break
        fb = "；".join(f.get("note", "") for f in crit)
        work_history.append((plan, note, f"[自审需修正] {fb}"))
        messages = build_messages(request, duration, skill_context, work_history, ops, transcript, source_facts)
        plan, note = plan_once(config, messages, tools, supported, duration)

    # apply_style 自带字幕：若同时冒出 add_subtitles，去掉后者，避免双字幕
    if any(o.get("type") == "apply_style" for o in plan):
        plan = [o for o in plan if o.get("type") != "add_subtitles"]

    art = build_edit_decisions(plan, Path(video_path) if video_path else Path("input.mp4"), duration, note)
    return {
        "edit_operations": plan,
        "summary": note or "已规划编辑方案",
        "edit_decisions": art,
        "review_findings": findings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print('用法: uv run python -m whatsapp_mvp.agent_editor <video.mp4> "<需求>"')
        sys.exit(1)
    video = Path(sys.argv[1])
    request = sys.argv[2]
    if not video.exists():
        print(f"找不到视频: {video}")
        sys.exit(1)

    config = get_config()
    duration = pr._probe_duration(video)
    print(f"视频: {video.name}  时长: {duration:.1f}s\n需求: {request}")

    # script 阶段：转录原始视频，供 agent 识别重复句/口误做 remove_segment
    print("\n=== Script 阶段：转录原始视频 ===")
    transcript = pr.transcribe_segments(str(video), video.parent / "_agent_out")
    print(f"转录 {len(transcript)} 段" if transcript else "转录为空/失败，继续（无转录感知）")

    print("\n=== Rule Zero：读 manifest + 白名单 + edit-director + Layer3 skill ===")
    manifest = load_manifest()
    allowed = allowed_tools_from_manifest(manifest)
    ops = allowed_ops(allowed)
    rfocus = review_focus_for(manifest, "edit")
    print(f"manifest 允许的工具: {sorted(allowed) if manifest else '(未读到 manifest，不限制)'}")
    print(f"可用操作(经白名单过滤): {ops}")
    skill_context = load_pipeline_context(ops)
    tools = build_tools(ops)
    supported = set(ops)

    history: list = []
    while True:
        print("\n=== Agent 规划中（已载入 skill，工具模拟执行）... ===")
        messages = build_messages(request, duration, skill_context, history, ops, transcript)
        plan, note = plan_once(config, messages, tools, supported, duration)

        # Reviewer：critical 自动修，最多 2 轮
        for _ in range(2):
            findings, verdict = review_plan(config, request, plan, note, rfocus)
            criticals = [f for f in findings if f.get("severity") == "critical"]
            if not criticals:
                break
            fb = "；".join(f.get("note", "") for f in criticals)
            print(f"  [自审] 发现 critical，自动修订：{fb}")
            history.append((plan, note, f"[自审需修正] {fb}"))
            messages = build_messages(request, duration, skill_context, history, ops)
            plan, note = plan_once(config, messages, tools, supported, duration)

        if not plan:
            print("Agent 没有规划出可执行操作。")
            if note:
                print(f"Agent 说明: {note}")
            return

        # 产出并校验 edit_decisions
        art = build_edit_decisions(plan, video, duration, note)
        ok, err = validate_edit_decisions(art)

        print("\n=== Agent 规划的剪辑方案（edit_decisions） ===")
        for i, op in enumerate(plan, 1):
            args = {k: v for k, v in op.items() if k != "type"}
            print(f"  {i}. {op['type']}  {args if args else ''}")
        if note:
            print(f"\n剪辑决策依据: {note}")
        other = [f for f in findings if f.get("severity") != "critical"]
        if other:
            print("自审意见: " + "；".join(f"[{f.get('severity')}] {f.get('note')}" for f in other))
        print(f"edit_decisions schema 校验: " + ("通过" if ok else ("跳过" if ok is None else f"未通过 - {err}")))

        ans = input("\n[y]执行  [n]取消  或直接输入修改意见: ").strip()
        low = ans.lower()
        if low in ("y", "yes", "confirm", "执行"):
            print("\n=== 执行中 ===")
            out = execute_plan(video, plan, art)
            print(f"\n完成 → {out}\nedit_decisions → {video.parent / '_agent_out' / 'edit_decisions.json'}")
            return
        if low in ("n", "no", "cancel", "取消", ""):
            print("已取消。")
            return
        history.append((plan, note, ans))
        print(f"\n收到修改意见，重新规划：{ans}")


if __name__ == "__main__":
    main()
