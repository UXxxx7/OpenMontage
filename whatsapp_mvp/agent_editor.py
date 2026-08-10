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
    "color_grade": ["color_grade"],
    "insert_broll": ["video_compose"],
    "add_music": ["audio_mixer"],  # manifest 的 compose 阶段已声明此工具（"audio layering + segmented music"）
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
    "color_grade": ("整片调色/画面质感（ffmpeg 预设）。profile：cinematic_warm=电影暖调, "
                    "cinematic_cool=冷调青橙, moody_dark=暗黑氛围, bright_clean=明亮干净, "
                    "vintage_film=复古胶片, high_contrast=高对比, neutral=轻微校色。"
                    "intensity 0~1 混合度（默认 0.85）",
                    {"profile": {"type": "string", "enum": ["cinematic_warm", "cinematic_cool",
                     "moody_dark", "bright_clean", "vintage_film", "high_contrast", "neutral"]},
                     "intensity": {"type": "number"}}, []),
    "insert_broll": ("把 b-roll 叠进成片（默认 b-roll 铺满、人物缩右下小窗，保留原声）。b-roll 两种来源："
                     "①用户上传的素材（source_facts 里列出了 [b<编号>]）→ 该 items 项给 asset_ref；"
                     "②用户明确要求 AI 生成 b-roll、或用户对某一处点名要生成（哪怕别处有上传素材） → 该 items 项给 gen_prompt（英文画面描述）。"
                     "同一 items 项 asset_ref 与 gen_prompt 二选一，不要同时给。items 每项："
                     "asset_ref=素材编号（见事实里的 [b<编号>]），gen_prompt=AI 生成 b-roll 的英文画面描述，"
                     "gen_provider=生成 provider（默认 omni），start_seconds/end_seconds=放置时间窗，"
                     "mode=broll_main(默认,b-roll主+人物小窗)|cutaway(b-roll全屏无人物)|pip(人物主+b-roll小窗)。"
                     "orientation=auto(默认,方向跟随素材)|portrait|landscape（用户明说竖屏/横屏时才设）",
                     {"items": {"type": "array", "items": {"type": "object", "properties": {
                        "asset_ref": {"type": "integer"},
                        "gen_prompt": {"type": "string"},
                        "gen_provider": {"type": "string", "enum": ["omni"]},
                        "gen_force": {"type": "boolean"},
                        "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"},
                        "mode": {"type": "string", "enum": ["broll_main", "cutaway", "pip"]}}}},
                      "orientation": {"type": "string", "enum": ["auto", "portrait", "landscape"]}},
                     ["items"]),
    "add_music": ("配一段背景音乐，压低音量垫在说话人原声底下（不改变原声响度）。"
                 "**仅当用户明确要求背景音乐/BGM/配乐时才用，不要主动加**——音乐可能喧宾夺主、"
                 "不合用户口味，不能替用户做这个决定。query=检索背景音乐的英文关键词（描述曲风/"
                 "氛围，如 'calm ambient corporate' 'upbeat acoustic guitar'，必须英文，"
                 "provider 只认英文检索词），provider=检索 provider（默认 pixabay，免费曲库检索，"
                 "一般不用写），volume=背景音乐相对音量 0.05-0.4（默认约 0.18，明显是垫底的分量；"
                 "用户说'再小声/再大声点'才按需调，不写就用默认）。",
                 {"query": {"type": "string"}, "provider": {"type": "string", "enum": ["pixabay"]},
                  "volume": {"type": "number"}},
                 ["query"]),
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
- 只能用**给你的工具列表**里的工具（已按 manifest 的 tools_available 过滤）；用户要的、工具做不到的（翻译字幕、换背景等），不要硬凑，在 finish 的 rationale 里点出做不了。
- 字幕(add_subtitles)若用，放最后。
- 关于停顿：用户若说"太紧/不自然/保留正常停顿"之类，不要直接放弃去静音，而是给 remove_silences 传更大的 min_silence_duration（如 1.0~1.5 秒），只剪明显偏长的静音、保留自然的句间停顿。
- 口误清理是剪辑的基本功，不是需要用户额外点名的"加分项"：只要转录里能看出明显口误/语气词/卡壳重录（呃/嗯/说错重说/半句话重来），默认在方案里加 remove_filler——不管用户这次具体要求的是什么（哪怕只说了"要好看/加音乐"这类点名到某个环节的诉求也一样加），除非用户明确说"保留原始/不要剪口误/原样剪辑"才不加。这跟 add_music/insert_broll 这类需要用户主动点名的"增值"操作不同：去掉说话人自己都不想留下的口误，是任何"剪一下/剪成片/剪好看"类请求的隐含预期，专业剪辑师不会因为客户只说了"调个色"就把明显的卡壳留在成片里（2026-07-16 真实反馈：用户说"要好看，加上音乐"，方案里因为"用户没要求"就完全没剪口误，体验很差）。优先用 remove_filler（它读转写逐词判断，能剪有声的"呃/嗯"和无停顿的重录），不要用 remove_segment 去手动框口误。remove_silences 触发条件不变、仍然只在用户明确要求"更紧凑/去停顿"时才加——停顿是否要剪更主观（可能是刻意的停顿节奏），不该跟口误一样默认处理；remove_silences 和 remove_filler 可叠加。
- 基于内容的"选择性剪辑"：若提供了逐句转录（带时间戳），当用户要"只保留讲 X 的部分""删掉聊 Y 那段"这类**按内容选择**的诉求，用 remove_segment(start_seconds, end_seconds) 按转录里的真实时间戳精确剪除，可多次调用。没有明确要求就不要擅自删改说话内容。
- 调色/画面质感：用户说"调暖/暖色/电影感"→ color_grade profile=cinematic_warm；"冷调/青橙"→ cinematic_cool；"暗黑/氛围感/压暗"→ moody_dark；"明亮干净/清新"→ bright_clean；"复古/胶片/怀旧"→ vintage_film；"高对比/浓郁/有冲击力"→ high_contrast；"轻微校色/自然"→ neutral。color_grade 是整片调色，可与剪辑类叠加；但**不要和 apply_style 叠加**（模板自带风格观感，套模板时就别再单独调色）。
- b-roll 插入：source_facts 里每列一段 b-roll，就在计划里对应放一条 insert_broll 的 items 项（没列则不放）。上传带说明的素材本身即为"要插入"的指令，按此正常执行即可，不需要在 summary 里论证、也不要与自审意见辩论。放置：用素材说明（label）去转录里找内容匹配的那句，取其时间窗 {asset_ref: 素材编号, start_seconds, end_seconds}。mode 默认 broll_main（b-roll 铺满主屏、人物缩右下小窗）；仅用户明说"人物为主"才 pip、"全屏不要人物"才 cutaway。orientation 默认 auto（主视频或任一 b-roll 是横屏就出横屏，否则竖屏）；仅用户明说竖屏/横屏时才设。说明实在匹配不上才跳过该段；两段不重叠；没给具体位置就按说明与转录话题就近放置。
- AI 生成 b-roll（gen_prompt）：每一处 b-roll 插入是"用上传素材"还是"AI 生成"，**按用户对这一处的措辞分别判断**：用户说"插入视频X/用这段素材/放我传的那段"→该处用 asset_ref；用户说"生成一段/AI 做一个/我没有素材你生成"→该处用 gen_prompt。**关键：即使用户为别的位置上传了素材，只要用户明确要求"生成"某一处，该处就必须用 gen_prompt，绝不能拿上传素材去顶替那一处**（例："claudecode 处插入视频2、vscode 处生成一段 3s"→前者一条 asset_ref item、后者一条 gen_prompt item，两条不同来源并存）。gen_prompt 必须写**英文**（生成模型只认英文），简洁具体地描述画面（如 "a developer typing code in VS Code, close-up of the screen, warm lighting"），放在转录里话题相关的时间窗（start_seconds/end_seconds）。gen_provider 默认 omni，一般不用写。**不主动生成**：用户没对某处明确说"生成/AI 做"就不要用 gen_prompt（生成有真实成本，约 $0.10/秒）；只上传了素材、没提生成的普通情况，照常全用 asset_ref。生成的 b-roll 在同一 job 内会缓存复用（用户改别处、重新 confirm 都不会重复生成、也不重复计费）；**仅当用户明确说"重新生成/换一段/重做这段 b-roll"时**，才在该 gen_prompt 项上加 gen_force=true 强制重生成，其余任何情况（含普通改方案）一律不要加 gen_force。
- 背景音乐：**仅当用户明确要求"加背景音乐/配个 BGM/加点音乐"这类诉求时**才放一条 add_music，不要主动加——不是每条视频都适合配乐，音乐可能喧宾夺主，这是用户的选择不是默认行为。query 必须写**英文**关键词描述曲风/氛围（如用户说"轻快一点的"→ 'upbeat acoustic guitar'，"沉稳专业的"→ 'calm ambient corporate'），没给具体曲风就按视频内容基调合理猜一个。默认走免费曲库检索（provider=pixabay，不用写），volume 不用写，除非用户明确要求音量大小。
- 出片风格：用户说"剪好看点/精致/小红书风格/我们的品牌风格/做正式些"这类诉求，用 apply_style 套品牌模板出片。它**自带字幕**，选了它就不要再加 add_subtitles。
- 零指令默认：用户只说"帮我剪一下/剪一下/edit this"这类**没有任何具体指令**的请求，不要问澄清，直接默认加 apply_style（remove_filler 已经是任何请求都会加的基本功，见上条，这里不用重复说明），并在 finish 的 rationale 里说明这是默认处理、下次可给更具体要求。
- **语言**：rationale/summary 必须和用户这句需求用的是同一种语言——用户打中文就整段中文，用户打英文就整段英文，不要中英混杂、不要不管用户说什么都用同一种语言回。判断依据是用户这次输入本身的语言，不是你训练时更常见哪种语言。

- 输出纪律（重要）：每个编辑动作都必须**调用对应的工具**落成一个操作——**绝不能只在 rationale/summary 里用文字描述方案而不实际调用工具**（那会得到一个空计划、等于没剪）。在**确保操作已通过工具调用产出**的前提下，rationale/summary **必须写完整方案 + 依据 + 具体时间点**：逐条列出每一步做了什么、为什么（依据用户哪句要求或哪条规则），并给出涉及的时间点——去口误/去静音剪掉了哪几段（如"3.4→4.3s、11.6→12.4s"）、每段 b-roll 对应转录里的哪句、放在哪个时间窗（如"b1 对应 5.9s 处 VS Code 那句 → 放 5.9–12.0s"）。这份详细说明是给用户在确认前审阅、并据此打字微调用的，务必写全。收到自审(review)意见时，据此调整**要调用的工具**，不要在 rationale 里反驳规则或复述条文。

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


# 原生 provider 端点（无 LLM_BASE_URL / 中转站时兜底直连，和 llm_planner.py 的
# provider 路由保持一致）。两者都是 OpenAI 兼容的 chat/completions + function-calling，
# 跟这里的 tool-calling 循环直接兼容；claude 的原生 Messages API 工具格式不同，
# 暂不在此路径支持——要用就配 LLM_BASE_URL 走中转站。
_NATIVE_PROVIDER_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}


def _resolve_endpoint_and_key(config) -> tuple[str | None, str | None]:
    base_url = (config.llm_base_url or "").rstrip("/")
    if base_url:
        endpoint = base_url if base_url.endswith("/v1") else base_url + "/v1"
        return endpoint + "/chat/completions", config.llm_api_key
    provider = (config.llm_provider or "").lower()
    endpoint = _NATIVE_PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        return None, None
    api_key = config.llm_api_key or config.deepseek_api_key or config.openai_api_key
    return endpoint, api_key


def _chat(config, messages, tools=None, json_mode=False):
    endpoint, api_key = _resolve_endpoint_and_key(config)
    if not endpoint or not api_key:
        raise RuntimeError(
            "需要配置 LLM_BASE_URL（中转站）或原生 provider（LLM_PROVIDER=deepseek/openai + 对应 API key）")
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
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=90)
        if resp.status_code == 429 and attempt < 3:
            wait = 25 * (attempt + 1)
            import logging as _logging
            _logging.getLogger(__name__).warning(f"L2 LLM 429，{wait}s 后重试")
            _time.sleep(wait)
            continue
        if resp.status_code >= 400 and attempt < 3 and (
                "Upstream request failed" in resp.text or "upstream_error" in resp.text):
            # 网关上游瞬时故障被包装成 4xx(实测同一请求成功率仅 20-60%)——
            # 与 schema/内容无关,短退避重试;真正的请求体错误不含此标记,照旧直接抛。
            wait = 3 * (attempt + 1)
            import logging as _logging
            _logging.getLogger(__name__).warning(
                f"L2 LLM {resp.status_code} 上游瞬时失败，{wait}s 后重试({attempt + 1}/3)")
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

def _default_plan(supported: set) -> list:
    """确定性默认计划：L2 多次返回空计划时的地板（不回退 L1.5）。只用受支持的基础剪辑。"""
    plan = [{"type": t} for t in ("remove_filler", "remove_silences", "add_subtitles") if t in supported]
    if not plan:
        plan = [{"type": "remove_silences"}]  # 极端兜底：至少去个静音，不交付原片
    return plan


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
    messages = build_messages(request, duration, skill_context, work_history, ops, transcript, source_facts)
    plan, note = plan_once(config, messages, tools, supported, duration)

    # 空计划重试：模型偶尔只写说明不调用工具。用干净上下文（丢掉可能诱发"辩论"的历史）
    # 强制重试，最多 2 次。始终留在 L2，不回退 L1.5。
    _empty_retries = 0
    while not plan and _empty_retries < 2:
        _empty_retries += 1
        import logging as _logging
        _logging.getLogger(__name__).warning(f"L2 返回空计划，第 {_empty_retries} 次重试（不回退 L1.5）")
        msgs_clean = build_messages(request, duration, skill_context, [], ops, transcript, source_facts)
        plan, note = plan_once(config, msgs_clean, tools, supported, duration)

    findings: list = []
    for _ in range(2):
        findings, verdict = review_plan(config, request, plan, note, rfocus)
        crit = [f for f in findings if f.get("severity") == "critical"]
        if not crit:
            break
        fb = "；".join(f.get("note", "") for f in crit)
        work_history.append((plan, note, f"[自审需修正] {fb}"))
        messages = build_messages(request, duration, skill_context, work_history, ops, transcript, source_facts)
        new_plan, new_note = plan_once(config, messages, tools, supported, duration)
        # 重规划若返回空操作（模型改去写说明/辩论），不接受——保留上一版有操作的计划
        if not new_plan:
            import logging as _logging
            _logging.getLogger(__name__).warning("自审重规划返回空操作，保留上一版计划")
            break
        plan, note = new_plan, new_note

    # 兜底：多次重试仍空，就用确定性默认计划（去口误+去静音+字幕）。始终留在 L2、
    # 不回退 L1.5，也绝不把未剪的原片当成片交付。b-roll 需 LLM 做标签↔转录匹配，
    # 确定性地板不含 b-roll（此地板极少触发——正常路径与重试几乎总能出真计划）。
    if not plan:
        import logging as _logging
        _logging.getLogger(__name__).warning("L2 多次重试仍返回空计划，使用确定性默认计划")
        plan = _default_plan(supported)
        # 即使回退默认方案，也要向用户说明理由（不静默兜底）。若模型上一版留了说明，
        # 用它当原因；否则给通用原因。
        _name = {"remove_filler": "去口误", "remove_silences": "去静音", "add_subtitles": "加字幕"}
        _done = "、".join(_name.get(o.get("type"), str(o.get("type"))) for o in plan)
        _reason = (note.strip() if note and note.strip()
                   else "规划器多次没能给出具体的剪辑动作（常见于转录为空、或素材说明与视频内容对不上）")
        note = (f"未能生成定制方案，原因：{_reason}。已按默认方案处理（{_done}）。"
                "如需插入 b-roll 或更精细的剪辑，请把主视频/各 b-roll 的说明重发一次。")

    # apply_style 自带字幕与整体观感：若同时冒出 add_subtitles / color_grade，去掉它们
    # （模板拥有最终字幕和调色，避免双字幕 / 双重调色）
    if any(o.get("type") == "apply_style" for o in plan):
        plan = [o for o in plan if o.get("type") not in ("add_subtitles", "color_grade")]

    # b-roll 守卫：素材事实里给了 b-roll（source_facts 含 insert_broll 指令），但最终
    # 计划里一条 insert_broll 都没有 —— 不静默丢弃，明确告诉用户没插入以及可能的原因。
    if source_facts and "insert_broll" in source_facts and not any(
            o.get("type") == "insert_broll" for o in plan):
        _extra = ("注意：你上传的 b-roll 这次没有被插入——通常是某段素材的说明没能和主视频"
                  "转录里真实说过的话对应上（比如说明写的话主视频里没出现，或把两段的说明"
                  "顺序搞反了）。请确认每段 b-roll 的说明指向主视频里真正说过的那句，然后重发。")
        note = f"{note} {_extra}" if note else _extra

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