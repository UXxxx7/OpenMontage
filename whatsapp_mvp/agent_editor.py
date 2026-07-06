"""L2 原型（AGENT_GUIDE 对齐版）：agent 工具调用循环，复用 OpenMontage handler。

对齐 AGENT_GUIDE.md 的治理：
- Rule Zero：动手前把 pipeline manifest(talking-head.yaml) + edit-director skill 读进上下文；
- 产出 edit_decisions 风格的方案（artifact）；
- Human Checkpoint：确认门支持 approve / 取消 / **提意见修订** 三支循环。

独立脚本，不接 WhatsApp。流程：读规范 -> 规划(模拟) -> 展示/修订 -> 确认 -> 执行。

用法：
    uv run python -m whatsapp_mvp.agent_editor <video.mp4> "把这条剪成适合抖音发布的"
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import requests

from . import pipeline_runner as pr
from .config import get_config


# ---------------------------------------------------------------------------
# Rule Zero：读入 pipeline manifest + stage director skill
# ---------------------------------------------------------------------------

def load_pipeline_context() -> str:
    """把 talking-head 的 manifest 和 edit-director skill 读进来，注入 agent 上下文。"""
    root = Path(get_config().openmontage_root)
    parts: list[str] = []

    manifest = root / "pipeline_defs" / "talking-head.yaml"
    if manifest.exists():
        parts.append(
            "### Pipeline manifest（talking-head.yaml）\n"
            "以下是本 pipeline 的权威定义（stage、可用工具、审查重点、审批门）。"
            "你的规划只能用 edit/compose 阶段允许的工具。\n```yaml\n"
            + manifest.read_text(encoding="utf-8")[:6000] + "\n```"
        )
    skill = root / "skills" / "pipelines" / "talking-head" / "edit-director.md"
    if skill.exists():
        parts.append(
            "### Edit stage director skill（edit-director.md）\n"
            "这是 edit 阶段的作业规范，你必须遵循它来规划剪辑决策：\n"
            + skill.read_text(encoding="utf-8")[:5000]
        )

    if not parts:
        return "（未找到 pipeline manifest / edit-director skill，按通用规范规划。）"
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 工具清单（function-calling schema）—— handler 注册表的 9 个操作
# ---------------------------------------------------------------------------

def _fn(name, desc, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required or []},
    }}


TOOLS = [
    _fn("trim_start", "删掉开头 N 秒", {"seconds": {"type": "number"}}, ["seconds"]),
    _fn("trim_end", "删掉结尾 N 秒", {"seconds": {"type": "number"}}, ["seconds"]),
    _fn("keep_range", "只保留 [start, end] 这一段", {
        "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}},
        ["start_seconds", "end_seconds"]),
    _fn("remove_segment", "删掉中间 [start, end] 这一段，保留其余", {
        "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}},
        ["start_seconds", "end_seconds"]),
    _fn("remove_silences", "去掉所有静音/停顿使视频更紧凑", {}),
    _fn("speed_up_silence", "把静音段加速而非删除", {"factor": {"type": "number"}}),
    _fn("trim_leading_silence", "只去掉开头的静音/空白", {}),
    _fn("reframe", "转换画幅：portrait=竖屏9:16, square=1:1, landscape=16:9, cinematic=21:9", {
        "aspect": {"type": "string", "enum": ["portrait", "square", "landscape", "cinematic"]}}),
    _fn("add_subtitles", "转写并烧录字幕（原语言）", {"language": {"type": "string"}}),
    _fn("finish", "编辑规划完成，无需更多操作。可在 rationale 里简述你的剪辑决策依据。",
        {"rationale": {"type": "string"}}),
]

SUPPORTED = {t["function"]["name"] for t in TOOLS} - {"finish"}

SYSTEM_BASE = """你是 OpenMontage 的剪辑 agent，负责编辑用户上传的 talking-head 视频。

治理要求（遵循 AGENT_GUIDE.md）：
- Rule Zero：动手前先读下面给出的 pipeline manifest 和 edit-director skill，按其规范规划。
- 只能用给你的这些工具；用户要的、工具做不到的（翻译字幕、配乐、调色、删口误重复等），不要硬凑，在 finish 的 rationale 里点出做不了。
- 字幕(add_subtitles)若使用，放在最后。

现在是"规划阶段"：工具是**模拟执行**的（只返回预计时长，不真剪）。按合理顺序调用工具达成用户目标，不确定位置时用给定的视频时长推算。规划完成后调用 finish，并在 rationale 里用一句话说明你的剪辑决策依据。"""


# ---------------------------------------------------------------------------
# 模拟执行（规划阶段，只估算时长）
# ---------------------------------------------------------------------------

def _simulate(name: str, args: dict, duration: float) -> tuple[dict, float]:
    d = duration
    if name in ("trim_start", "trim_end"):
        d = max(0.0, duration - float(args.get("seconds", 0)))
    elif name == "remove_segment":
        d = max(0.0, duration - (float(args.get("end_seconds", 0)) - float(args.get("start_seconds", 0))))
    elif name == "keep_range":
        d = max(0.0, float(args.get("end_seconds", duration)) - float(args.get("start_seconds", 0)))
    elif name == "remove_silences":
        d = round(duration * 0.85, 1)
    elif name == "speed_up_silence":
        d = round(duration * 0.9, 1)
    elif name == "trim_leading_silence":
        d = max(0.0, duration - 1.0)
    return {"ok": True, "estimated_duration_seconds": round(d, 1)}, d


# ---------------------------------------------------------------------------
# Agent 规划（tool-calling，单轮出一个完整方案）
# ---------------------------------------------------------------------------

def _chat(config, messages, tools):
    endpoint = (config.llm_base_url or "").rstrip("/")
    if not endpoint:
        raise RuntimeError("需要配置 LLM_BASE_URL（中转站）")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    endpoint += "/chat/completions"
    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {config.llm_api_key}", "Content-Type": "application/json"},
        json={"model": config.llm_model, "messages": messages, "tools": tools, "temperature": 0.2},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def build_messages(request: str, duration: float, skill_context: str, history: list) -> list:
    """构造对话消息；history 里每一项是 (上一版方案, 说明, 用户修改意见)。"""
    msgs = [{"role": "system", "content": SYSTEM_BASE + "\n\n" + skill_context}]
    msgs.append({"role": "user", "content": f"视频时长 {duration:.1f} 秒。用户需求：{request}"})
    for prev_plan, prev_note, feedback in history:
        summary = "；".join(
            f"{op['type']}(" + ", ".join(f"{k}={v}" for k, v in op.items() if k != "type") + ")"
            for op in prev_plan
        ) or "（无操作）"
        msgs.append({"role": "assistant", "content": f"上一版方案：{summary}。{prev_note or ''}"})
        msgs.append({"role": "user", "content": f"请根据以下修改意见，重新给出**完整**方案：{feedback}"})
    return msgs


def plan_once(config, messages: list, duration: float, max_steps: int = 8) -> tuple[list, str]:
    work = list(messages)
    planned: list[dict] = []
    cur = duration
    note = ""

    for _ in range(max_steps):
        data = _chat(config, work, TOOLS)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            note = msg.get("content") or ""
            break

        work.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})

        finished = False
        for tc in tool_calls:
            fname = tc["function"]["name"]
            try:
                fargs = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                fargs = {}

            if fname == "finish":
                finished = True
                note = fargs.get("rationale", "") or note
                sim = {"ok": True, "message": "规划结束"}
            elif fname in SUPPORTED:
                planned.append({"type": fname, **fargs})
                sim, cur = _simulate(fname, fargs, cur)
            else:
                sim = {"ok": False, "error": f"未知工具 {fname}"}

            work.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(sim, ensure_ascii=False)})

        if finished:
            break

    return planned, note


# ---------------------------------------------------------------------------
# 真正执行（确认后）—— 复用 pipeline_runner 的 handler
# ---------------------------------------------------------------------------

def execute_plan(video: Path, plan: list[dict], rationale: str) -> Path:
    workdir = video.parent / "_agent_out"
    workdir.mkdir(parents=True, exist_ok=True)

    # 产出 edit_decisions 风格 artifact（AGENT_GUIDE：每阶段留 canonical 记录）
    (workdir / "edit_decisions.json").write_text(
        json.dumps({"operations": plan, "rationale": rationale}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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
        args = {k: v for k, v in op.items() if k != "type"}
        print(f"  执行 {t} {args if args else ''}")
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
# CLI（确认门：approve / 取消 / 提意见修订）
# ---------------------------------------------------------------------------

def _show_plan(plan: list[dict], note: str):
    print("\n=== Agent 规划的剪辑方案（edit_decisions） ===")
    for i, op in enumerate(plan, 1):
        args = {k: v for k, v in op.items() if k != "type"}
        print(f"  {i}. {op['type']}  {args if args else ''}")
    if note:
        print(f"\n剪辑决策依据: {note}")


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

    print("\n=== 读取 pipeline manifest + edit-director skill（Rule Zero）... ===")
    skill_context = load_pipeline_context()

    history: list = []
    while True:
        print("\n=== Agent 规划中（已载入 skill，工具模拟执行）... ===")
        messages = build_messages(request, duration, skill_context, history)
        plan, note = plan_once(config, messages, duration)

        if not plan:
            print("Agent 没有规划出可执行操作。")
            if note:
                print(f"Agent 说明: {note}")
            return

        _show_plan(plan, note)

        ans = input("\n[y]执行  [n]取消  或直接输入修改意见: ").strip()
        low = ans.lower()
        if low in ("y", "yes", "confirm", "执行"):
            print("\n=== 执行中 ===")
            out = execute_plan(video, plan, note)
            print(f"\n完成 → {out}")
            return
        if low in ("n", "no", "cancel", "取消", ""):
            print("已取消。")
            return
        # 其余视为修改意见 → 记录并重规划
        history.append((plan, note, ans))
        print(f"\n收到修改意见，重新规划：{ans}")


if __name__ == "__main__":
    main()
