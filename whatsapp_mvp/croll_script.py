"""C-roll 文案生成：看一张照片，写一段适合数字人口播的文案。

跟 qa_answer.py 一个模式——按用户语言出对应语言的文案，LLM 不可用时给一句
诚实的兜底而不是让整条 C-roll 流程直接失败在这一步。

风格和结构不靠形容词描述，靠 prompts/ 下经人工确认的成品样本 few-shot：
- croll_reference_scripts.json：8 条通用保险文案（旧库，泛用兜底）。
- insurance_scripts_extended.json：28 条按险种分类、中英双语的文案（新库，
  2026-07-21 补充）。hint 命中具体险种（如"讲讲重疾险"/"talk about term
  life"）时优先从新库按 category + 目标语言选样本，选不中才退回旧库的关键词
  匹配。目标时长也跟着变——命中险种时用该险种样本的实测平均时长（新库普遍
  78-102 秒，比旧库默认的 60 秒更长），没命中才用固定 60 秒兜底，这样绝大多数
  情况下不用依赖后面的超长压缩兜底去硬砍。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .llm_client import call_llm_chat, call_vision_chat

logger = logging.getLogger(__name__)

_REFERENCE_PATH = Path(__file__).parent / "prompts" / "croll_reference_scripts.json"
_INSURANCE_LIBRARY_PATH = Path(__file__).parent / "prompts" / "insurance_scripts_extended.json"

_DEFAULT_DURATION_S = 60

# hint 里出现这些词就判定命中对应险种。顺序有讲究：越具体的越先判——"定期寿险"/
# "终身寿险"各自的关键词必须排在泛化的"寿险"前面，否则"我想讲定期寿险"会先被
# 泛化词匹配掉，导致后面精确的判断永远轮不到。泛化的"寿险"/"life insurance"
# 命中时两个寿险子类都算（生成时从两边一起挑样本），因为光看这几个字判断不出
# 用户到底想要哪种。
_CATEGORY_ALIASES: list[tuple[str, list[str]]] = [
    ("定期寿险", ["定期寿险", "定寿", "term life"]),
    ("终身寿险", ["终身寿险", "增额终身寿", "whole life"]),
    ("寿险", ["寿险", "life insurance"]),  # 泛化兜底，命中时展开成上面两个
    ("重疾险", ["重疾", "重大疾病", "critical illness"]),
    ("医疗险", ["医疗险", "百万医疗", "住院医疗", "医疗保险", "medical insurance",
              "health insurance", "hospitalization"]),
    ("意外险", ["意外险", "意外保险", "accident insurance", "accidental"]),
    ("年金险", ["年金险", "养老年金", "教育金", "annuity", "retirement plan", "education fund"]),
    ("车险", ["车险", "汽车保险", "auto insurance", "car insurance"]),
    ("家财险", ["家财险", "家庭财产险", "房屋保险", "home insurance", "property insurance"]),
]

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


def _load_scripts(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("scripts") or []
    except Exception as e:
        logger.warning(f"croll_script: 样本库读取失败（{path.name}，继续无样本生成）: {e}")
        return []


def _match_categories(hint: str) -> list[str]:
    """hint 命中哪个/哪些险种（见 _CATEGORY_ALIASES 顺序说明）。没命中返回空列表。"""
    lowered = hint.lower()
    for canonical, aliases in _CATEGORY_ALIASES:
        if any(a.lower() in lowered for a in aliases):
            return ["定期寿险", "终身寿险"] if canonical == "寿险" else [canonical]
    return []


def _pick_examples(hint: str, lang: str, k: int = 2) -> tuple[list[dict], list[str]]:
    """挑 few-shot 样本。命中具体险种就从新库按 category + lang 选（同语言不够
    就用同险种跨语言的凑数，毕竟样本只是学结构不是抄内容）；没命中险种就退回
    旧的通用库，按 hint 关键词匹配 title/audience，还挑不中就取前 k 条兜底。
    返回 (样本列表, 命中的险种列表)——险种列表用来决定目标时长。"""
    categories = _match_categories(hint)
    if categories:
        pool = [s for s in _load_scripts(_INSURANCE_LIBRARY_PATH) if s.get("category") in categories]
        same_lang = [s for s in pool if s.get("lang") == lang]
        if len(same_lang) >= k:
            return same_lang[:k], categories
        # 同语言不够凑数，跨语言样本补齐（仍是同险种，结构参考价值不打折）。
        others = [s for s in pool if s not in same_lang]
        return (same_lang + others)[:k], categories

    scripts = _load_scripts(_REFERENCE_PATH)
    if hint.strip():
        hits = [s for s in scripts
                if any(w and (w in s.get("title", "") or w in s.get("target_audience", ""))
                       for w in hint.strip().split())]
        if hits:
            return hits[:k], []
    return scripts[:k], []


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
                 duration_s: int | None = None) -> str | None:
    """看图写文案。hint 是用户给的额外提示（比如想推广什么险种、什么语气），
    可以为空——为空时完全由 AI 自由发挥，看图片本身像该说点什么。

    duration_s 不传时自动决定：hint 命中具体险种（如"重疾险"/"term life"）就用
    该险种样本的实测平均时长（新库普遍 78-102 秒），没命中就用 60 秒通用默认。
    调用方仍可显式传 duration_s 覆盖这个自动逻辑。

    成功返回文案字符串；LLM 不可用或调用失败返回 None（调用方应把这当
    "这步没成"处理，不硬造一段文案糊弄用户）。
    """
    hint = hint or ""  # 防御 hint=None：下面多处 .strip()/.lower() 都假设是字符串
    examples_list, categories = _pick_examples(hint, lang)
    examples = _format_examples(examples_list, lang)

    if duration_s is None:
        same_lang_in_pool = [s for s in examples_list if s.get("lang") == lang]
        durations = [s["duration_seconds"] for s in (same_lang_in_pool or examples_list)
                     if s.get("duration_seconds")]
        duration_s = round(sum(durations) / len(durations)) if durations else _DEFAULT_DURATION_S
        if categories:
            logger.info(f"croll_script: hint 命中险种 {categories}，目标时长自动定为 {duration_s}s")

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

    result, _usage = call_vision_chat(prompt, [image_path])
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
        result, _usage = call_llm_chat(system, ask, json_mode=False)
        result = (result or "").strip()
        return result or None
    except Exception as e:
        logger.warning(f"croll_script: 压缩调用失败: {e}")
        return None
