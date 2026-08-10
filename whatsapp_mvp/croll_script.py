"""C-roll 文案生成：看一张照片，写一段适合数字人口播的文案。

跟 qa_answer.py 一个模式——按用户语言出对应语言的文案，LLM 不可用时给一句
诚实的兜底而不是让整条 C-roll 流程直接失败在这一步。

默认目标时长 60 秒（之前是 15-25 秒的短口播）。风格和结构不靠形容词描述，
靠 prompts/croll_reference_scripts.json 里经人工确认的成品样本 few-shot——
样本按 hint 关键词挑最相关的，挑不中就取前两条兜底。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .llm_client import call_llm_chat, call_vision_chat

logger = logging.getLogger(__name__)

_REFERENCE_PATH = Path(__file__).parent / "prompts" / "croll_reference_scripts.json"

# 参考样本实测语速：中文约 4 字/秒，英文约 2.5 词/秒（INS_SCRIPT_001~008，
# 55-64 秒对应 210-260 字）。按目标时长换算出字数区间给模型。
_ZH_CHARS_PER_SECOND = 4.0
_EN_WORDS_PER_SECOND = 2.5

_PROMPT_ZH = (
    "这张照片要用来生成一条数字人口播视频（AI 会让照片里的人物说话）。"
    "请你看图写一段第一人称口播文案，自然口语化、像真人在对着镜头说话，"
    "不要有舞台提示或旁白说明，只输出要说的话本身。\n"
    "{hint_line}"
    "长度控制在 {min_chars}-{max_chars} 字（约 {duration} 秒的语速）。\n"
    "\n"
    "参考下面的成品文案样本，学它们的结构和口吻（不要抄内容）：\n"
    "1. 开头一句就抓住注意力——反常识判断（\"很多人以为…其实不对\"）或直接向观众提问；\n"
    "2. 先重新定义主题的本质，再展开；\n"
    "3. 通篇用具体数字和实例支撑，不说空话；\n"
    "4. 关键专业术语可以中英混排（如 'cash lump sum'）；\n"
    "5. 给出明确的专业建议或行动逻辑；\n"
    "6. 结尾一句金句收束，常用\"记住，…\"式句型或有温度的价值升华。\n"
    "\n"
    "{examples}"
)
_PROMPT_EN = (
    "This photo will be used to generate a talking-head video (AI will animate "
    "the person in the photo to speak). Look at the photo and write a "
    "first-person spoken script — natural and conversational, like someone "
    "talking directly to camera. Output only the spoken words themselves, no "
    "stage directions or narration labels.\n"
    "{hint_line}"
    "Keep it to {min_words}-{max_words} words (roughly {duration} seconds spoken).\n"
    "\n"
    "Model the structure and tone (not the content) on the sample scripts below:\n"
    "1. Open with an attention hook — a counterintuitive claim or a direct question;\n"
    "2. Redefine what the topic is really about before expanding;\n"
    "3. Back every point with concrete numbers and examples, no fluff;\n"
    "4. Give clear professional advice or an action rule;\n"
    "5. Close with one memorable line.\n"
    "\n"
    "{examples}"
)


def _load_reference_scripts() -> list[dict]:
    try:
        data = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
        return data.get("scripts") or []
    except Exception as e:
        logger.warning(f"croll_script: 参考样本库读取失败（继续无样本生成）: {e}")
        return []


def _pick_examples(scripts: list[dict], hint: str, k: int = 2) -> list[dict]:
    """按 hint 关键词命中 title/audience 挑样本；挑不中取前 k 条。样本的作用是
    示范结构和口吻，领域对不上也比没有强。"""
    if hint.strip():
        hits = [s for s in scripts
                if any(w and (w in s.get("title", "") or w in s.get("target_audience", ""))
                       for w in hint.strip().split())]
        if hits:
            return hits[:k]
    return scripts[:k]


def _format_examples(examples: list[dict], lang: str) -> str:
    if not examples:
        return ""
    header = "成品样本：" if lang == "zh" else "Sample scripts:"
    blocks = []
    for i, ex in enumerate(examples, 1):
        blocks.append(f"【样本{i}】{ex['script']}" if lang == "zh"
                      else f"[Sample {i}] {ex['script']}")
    return header + "\n" + "\n\n".join(blocks)


def write_script(image_path: str, lang: str = "zh", hint: str = "",
                 duration_s: int = 60) -> str | None:
    """看图写文案。hint 是用户给的额外提示（比如想推广什么、什么语气），
    可以为空——为空时完全由 AI 自由发挥，看图片本身像该说点什么。
    成功返回文案字符串；LLM 不可用或调用失败返回 None（调用方应把这当
    "这步没成"处理，不硬造一段文案糊弄用户）。
    """
    examples = _format_examples(_pick_examples(_load_reference_scripts(), hint), lang)

    if hint.strip():
        hint_line = (f'用户给的方向提示："{hint.strip()}"，围绕这个来写。\n'
                    if lang == "zh" else
                    f'The user gave this direction: "{hint.strip()}" — write around it.\n')
    else:
        hint_line = ""

    if lang == "zh":
        prompt = _PROMPT_ZH.format(
            hint_line=hint_line, duration=duration_s,
            min_chars=int(duration_s * _ZH_CHARS_PER_SECOND * 0.9),
            max_chars=int(duration_s * _ZH_CHARS_PER_SECOND * 1.1),
            examples=examples,
        )
    else:
        prompt = _PROMPT_EN.format(
            hint_line=hint_line, duration=duration_s,
            min_words=int(duration_s * _EN_WORDS_PER_SECOND * 0.9),
            max_words=int(duration_s * _EN_WORDS_PER_SECOND * 1.1),
            examples=examples,
        )

    result = call_vision_chat(prompt, [image_path])
    if not result or not result.strip():
        logger.warning("croll_script: 视觉 LLM 不可用或未返回内容，C-roll 文案生成失败")
        return None
    result = result.strip()

    # 实测带 hint + few-shot 时模型经常明显超写（60s 目标写出 400+ 字，HeyGen
    # 按时长线性计费，超一倍就多花一倍钱），超出上限 15% 就压缩一轮。压缩失败
    # 不算致命——超长文案仍然可用，记一条警告放行。
    limit = int(duration_s * _ZH_CHARS_PER_SECOND * 1.1) if lang == "zh" \
        else int(duration_s * _EN_WORDS_PER_SECOND * 1.1)
    length = len(result) if lang == "zh" else len(result.split())
    if length > limit * 1.15:
        logger.info(f"croll_script: 文案超长（{length}，上限 {limit}），压缩一轮")
        compressed = _compress(result, limit, lang)
        if compressed:
            return compressed
        logger.warning(f"croll_script: 压缩失败，放行超长文案（{length}）")
    return result


def _compress(script: str, limit: int, lang: str) -> str | None:
    unit = "字" if lang == "zh" else "words"
    system = ("你是口播文案编辑，只输出改写后的文案本身，不加任何说明。"
              if lang == "zh" else
              "You are a script editor. Output only the rewritten script, nothing else.")
    ask = (f"把下面这段口播文案压缩到 {limit} {unit}以内：保留开头的抓注意力句、"
           f"核心数字和结尾金句，删掉次要展开和重复表述，保持口语化：\n\n{script}"
           if lang == "zh" else
           f"Compress this spoken script to under {limit} {unit}. Keep the hook, "
           f"the key numbers, and the closing line; cut secondary elaboration:\n\n{script}")
    try:
        result = call_llm_chat(system, ask, json_mode=False)
        result = (result or "").strip()
        return result or None
    except Exception as e:
        logger.warning(f"croll_script: 压缩调用失败: {e}")
        return None
