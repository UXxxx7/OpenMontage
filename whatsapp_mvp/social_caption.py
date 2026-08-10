# WhatsApp MVP - Social media post caption + hashtag generator
#
# 生成的是发帖配文（IG/Facebook 帖子正文，含 hashtag），不是字幕/口白文案——
# 跟 croll_script.py（生成给数字人念的口播稿）是完全不同的产物类型，不能共用
# 那边的样本库（那边是长段第一人称口播稿，内地话术腔调，直接借来当帖文的
# few-shot 会把内地腔调带进港式帖文里，正好跟"不要听起来像 AI 腔"这个目标
# 反着来）。
#
# 目标用户是香港的保险从业员/KOL，成败标准是"读起来像真人发的帖，不是 AI
# 摘要"——所以这个模块的大部分篇幅在 prompt 设计（禁用套话清单、繁體字
# 硬性要求、港式口语用字）而不是调用逻辑本身。调用逻辑跟 croll_script.py/
# content_planner.py 是同一套既有约定：LLM 不可用或解析失败就返回 None，
# 调用方按"这步没成"处理，绝不编一段文案糊弄用户。

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from pathlib import Path
from typing import Optional

from .llm_client import call_llm_chat

logger = logging.getLogger(__name__)

# 只用繁體字是硬性要求，但实测证明光靠 prompt 管不住——真人测试抓到过模型
# 生成"一个"/"呢个"混入简体字（job_4e8504467ab6）。这里加一道机械的最终防线：
# 不管模型有没有听话，产出前统一过一遍简转繁，硬性保证结果一定是繁體，而不是
# 只在 _lint_caption 里留一条 warning 指望以后自己会好。s2t（通用简转繁）而不是
# s2hk（香港政府"常用字字形表"官方字形）——实测两者对同一批字输出不同字形
# （"説" vs "說"），s2t 给出的是香港人日常用输入法打字时实际会打出来的字形
# （說），s2hk 给出的是官方字形表异体（説）——这段文案是给人在 WhatsApp/IG
# 上照抄照贴的，要贴近日常实际写法，不是贴近政府公文标准。
try:
    from opencc import OpenCC
    _S2T_CONVERTER: Optional["OpenCC"] = OpenCC("s2t")
except Exception as e:  # 极端情况：包没装成功/初始化失败，不能让这个附加的
    # 安全网本身变成致命依赖——退回"不转换"，_lint_caption 的 warning 仍然
    # 是最后一道可观测信号。
    logger.warning(f"social_caption: OpenCC 初始化失败，简转繁安全网关闭: {e}")
    _S2T_CONVERTER = None


def _to_traditional(text: str) -> str:
    if _S2T_CONVERTER is None:
        return text
    try:
        return _S2T_CONVERTER.convert(text)
    except Exception as e:
        logger.warning(f"social_caption: 简转繁调用失败，放行原文: {e}")
        return text

_EXAMPLES_PATH = Path(__file__).parent / "prompts" / "social_caption_examples.json"

# 首个"创作型"LLM 调用——这个代码库其余地方全是 0.0-0.1（分类/结构化提取要
# 确定性），但确定性正好是"听起来像 AI"的成因之一（低温永远选"最保险的平均
# 答案"）。0.8 是未经生产数据验证的起点，见验证脚本人工评审后再调。
_TEMPERATURE = 0.8

# 单条 caption 只需要抓大意，不像 content_planner 的章节/数据点规划要逐句
# 对齐时间戳，上限比它的 12000 小很多。
_MAX_TRANSCRIPT_CHARS = 6000

_MAX_HASHTAGS = 6

# 生成整体的硬性 wall-clock 上限——call_llm_chat 内部本身最多可能重试 3 次
# （每次到 60s），这里再加一次失败解析重试（_call_llm_json 见下），最坏情况
# 叠起来可能到几分钟。这是附加功能，不能拖累用户真正在等的预览+编辑器链接，
# 所以在外面再包一层硬上限，超时就放弃（线程杀不掉，让它在后台跑完自生自灭，
# 结果直接丢弃——跟 llm_client.py 自己的 _post_bounded 是同一个取舍）。
_HARD_DEADLINE_S = 25
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="social-caption")

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

_SYSTEM_ZH = """你要为一段短视频写一条社交媒体帖文文案（IG/Facebook/小红书风格的帖子正文），\
不是视频里的口白，也不是字幕——是发布这条视频时配的那段文字，给人一眼看到、决定要不要点进去看的东西。

目标读者：香港保险从业员/KOL 的追随者。本地、口语化、看得懂"生活化"的表达。

硬性要求（一条都不能违反）：
1. 寫一條真正嘅帖文，唔係一段描述呢條片嘅文字。絕對唔可以出現"喺呢條片入面"、"呢條片講到"、"佢話"、\
"佢分享"呢啲旁述——呢啲係喺講述條片，唔係幫條片寫文案。除此之外語氣可以彈性選——揀返真人會點打嘅嗰種：\
係佢本人親自出嚟講就用第一人稱（"我見過好多次..."），係想直接同觀眾講就用"你"嚟開口（"你份保單30日後就\
到期"），如果口白內容係轉述第三方講嘅嘢，可以指名道姓再引用嗰句加返自己一句睇法。唔好硬套第一人稱——例如\
"我再講多次"呢類生硬嘅開場白（好似係接住之前講過嘅嘢，但其實條post係新㗎）就係要避免嘅嗰種生硬。跟返\
內容本身嘅語氣，唔好死跟一個固定模板。
2. 只用繁體字，不可以出现简体字（哪怕下面给的口白内容本身是简体，你的输出也必须是繁體）。
3. 用香港口語書面語——可以用「嘅」「啦」「喇」「咁」「唔」「佢」等助詞/用字，不要写成纯书面语的\
普通话腔调；专业术语用香港讲法（供款、保單、受保人），不要用内地讲法。
4. 文案里的每一句话都必须能在下面给的口白内容里找到根据——不要编造数字、不要编造这段视频没讲过的\
卖点或统计数据。
5. 篇幅要精簡——真實嘅帖文通常淨係 hashtag 之前 1-3 行短句，唔係一大段。揀口白入面最有力嗰一點做\
開頭，唔使逐點覆述晒。結構自由發揮，但大致係：一句抓住注意力的開頭（從口白實際內容出發，唔好泛泛而談）\
→ 最多多 1-2 行，換行要真實（用 \\n，不要写成一大段）→ 可以加一句自然的收尾（不是硬广告 CTA）。
6. 唔好用破折號（—），一次都唔好——用句號、逗號或者換行代替，真實用戶用手機打字幾乎唔會打破折號。
7. 對呢個讀者群嚟講表情符號好重要——喺文案入面自然咁用，唔好死死縮到得一兩個：可以用嚟開頭、標記重點、\
加強語氣。要同內容相關，唔係為咗擺而擺，但都唔使每一行都塞一個——要似真係KOL會發嘅帖，唔係企業腔，都唔係\
表情符號洗版。
8. 绝对不要出现这些老套/AI 腔调的说法（一个字都不能有）："在這個瞬息萬變的時代"、连续堆叠反问句\
（"你有沒有想過…你知唔知道…"）、清单式开头（"3個你一定要知道嘅原因"）、推销式收尾（"立即聯繫我了解更多"）、\
空洞的行业套话（除非口白原文真的这么说，否则不要写"人生無常，保障先行"这类空话）。
9. hashtag 要跟这段内容实际相关（提到的险种、话题、地区），不要塞 #fyp #instagood #viral 这类跟内容\
无关的通用标签。3-6 個之間，集中放喺文案最尾（唔好夾喺內文入面）。

只输出 JSON，不要 markdown，不要任何说明文字：
{"caption": "文案正文（换行用 \\n）", "hashtags": ["#标签1", "#标签2"]}"""

_SYSTEM_EN = """Write a social media post caption — the text that accompanies a published video post \
on Instagram/Facebook, NOT video narration, NOT subtitles — for a short video.

Target reader: followers of a Hong Kong insurance-agent KOL. Expect a professional-but-personable \
voice, not a generic influencer tone and not textbook marketing copy.

Hard requirements (none of these are negotiable):
1. Write a real caption, not a description of the video. NEVER write "in this video...", "in this \
clip...", "the speaker says/talks about...", "he/she explains..." — that's meta-narration about the \
footage, not a caption for it. Past that, the voice is flexible — use whichever a real person would \
actually type: first person when it's genuinely the poster's own take ("I've seen this play out a \
dozen times..."), direct address to the reader ("your policy renews in 30 days"), or naming the person \
and quoting them plus a short reaction if the transcript is someone else's words ("Jason Pargin nails \
it: '...'"). Don't force first person where it doesn't fit — a stilted callback like "I'll say it \
again" (implying the caption is continuing an earlier conversation it never started) is exactly the \
kind of forced voice to avoid. Match the register of the content, not a fixed template.
2. Every claim in the caption must be traceable to something actually said in the transcript below. \
Never invent statistics or selling points the video didn't actually make.
3. Keep it TIGHT. Real posts are usually 1-3 short lines before the hashtags, not a full paragraph — \
pick the single strongest idea from the transcript and lead with it, don't try to restate everything \
that was said. Structure is flexible, but roughly: a hook drawn from the transcript's real content (not \
a generic opener) -> at most 1-2 more lines, real line breaks (use \\n, don't write one dense paragraph) \
-> optionally one short natural sign-off (not a sales CTA).
4. No em dashes (—), anywhere. Use a period, a comma, or a line break instead — real KOL captions on \
phone keyboards essentially never use them.
5. Emojis matter for this audience — use them naturally and with a light hand throughout the caption \
(as a line-opener, to land a beat, for emphasis on the strongest point), not squeezed down to a token \
one-or-two. Keep them relevant to what's actually being said, not random decoration, and don't put one \
on every single line either — read like a real KOL post, not a corporate one and not an emoji-spam one.
6. Never use these clichés, not even once: "In today's fast-paced world...", "Let's dive in", stacked \
rhetorical questions ("Have you ever wondered... Did you know..."), listicle framing ("3 things you \
need to know"), corporate CTA closers ("Contact me today to learn more"), generic insurance platitudes \
not actually said in the transcript.
7. Hashtags must be relevant to what's actually in this video (the specific topic/product/region) — no \
generic padding like #fyp #instagood #viral. 3-6 hashtags, clustered at the end (not woven into the body).

Output ONLY valid JSON, no markdown, no explanation:
{"caption": "the caption text (use \\n for line breaks)", "hashtags": ["#tag1", "#tag2"]}"""

# 老套/AI 腔调用语黑名单——跟上面 prompt 里的负面约束是同一份清单，这里拿来
# 做生成后的机械检查（只记 warning，不拦截、不触发重跑：LLM 自我审查主观文风
# 这件事本身不可靠，尤其是几乎没有 few-shot 样本可对照的情况下——见样本库
# 的说明）。目的是给开发者/客户一个"模型是否经常犯这个毛病"的可观测信号，
# 不是质量门槛。
_BANNED_PHRASES_ZH = [
    "在這個瞬息萬變的時代", "在这个瞬息万变的时代",
    "你知唔知道", "你有無諗過", "你有沒有想過",
    "立即聯繫我了解更多", "立即联系我了解更多",
    "人生無常，保障先行",
    # 第三人称旁述——真人试用发现的真实回归项（robert-raw 测试：写成"呢條片入面
    # 個speaker話..."而不是第一人称），跟英文版的 "in this video"/"the speaker" 同一类。
    "喺呢條片入面", "喺呢条片入面", "呢條片講到", "呢条片讲到", "呢條片入面個", "片入面佢話",
]
_BANNED_PHRASES_EN = [
    "in today's fast-paced world", "let's dive in", "have you ever wondered",
    "contact me today to learn more", "3 things you need to know",
    # Third-person narration — a real regression caught in manual testing
    # (robert-raw: "In this clip, the speaker calls..." instead of first person).
    "in this video", "in this clip", "the speaker", "he explains", "she explains",
    "he talks about", "she talks about",
]


def _resolve_lang(edit_request: Optional[str]) -> str:
    """没有 job.lang 字段，也没有可靠的粤语/普通话识别（storage/jobs/ 里能找到
    同一段口白被前后两次转写分别标成 zho+简体 / zh+繁體的真实证据——转写的
    language 字段不可信，见验证记录）。所以不追求"识别语言"，只追求"跟这条
    对话目前在用的语言保持一致"——跟 Node 网关 resolveLang() 同一个默认方向
    （DEFAULT_LANG="zh"），不用 lang.py::detect_lang() 默认落到 "en" 的方向：
    那样会在最常见的"用户没打字、纯上传视频"场景下，跟整条对话的语言默认值
    反着来。"""
    text = (edit_request or "").strip()
    if _CJK_RE.search(text):
        return "zh"
    if text and re.search(r"[A-Za-z]", text):
        return "en"
    return "zh"


def _read_transcript_segments(job_dir: Path) -> Optional[list[dict]]:
    path = job_dir / "script_transcript.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"social_caption: 读取转写文件失败 {path}: {e}")
        return None
    segments = data.get("segments") or []
    return segments or None


def _build_transcript_text(segments: list[dict]) -> str:
    lines = [seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip()]
    text = " ".join(lines)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = text[:_MAX_TRANSCRIPT_CHARS] + " ...[transcript truncated]"
    return text


def _load_examples() -> list[dict]:
    try:
        data = json.loads(_EXAMPLES_PATH.read_text(encoding="utf-8"))
        return data.get("captions") or []
    except Exception as e:
        logger.warning(f"social_caption: 样本库读取失败，继续无样本生成: {e}")
        return []


def _pick_examples(lang: str, k: int = 3) -> list[dict]:
    # v1 只按语言过滤，不做险种匹配——样本库现在是空的，谈不上"匹配"，等
    # 客户提供的真实样本攒到每个险种至少 5 条左右，再照抄 croll_script.py 的
    # _match_categories 加分类匹配。
    pool = [c for c in _load_examples() if c.get("lang") == lang]
    return pool[:k]


def _format_examples(examples: list[dict], lang: str) -> str:
    if not examples:
        return ""
    header = "参考样本（学口吻和结构，不要抄内容）：" if lang == "zh" \
        else "Reference examples (match the tone and structure, don't copy the content):"
    blocks = []
    for i, ex in enumerate(examples, 1):
        tags = " ".join(ex.get("hashtags") or [])
        label = f"【样本{i}】" if lang == "zh" else f"[Example {i}]"
        blocks.append(f"{label}\n{ex.get('caption', '')}\n{tags}")
    return header + "\n\n" + "\n\n".join(blocks)


def _build_user_message(transcript_text: str, examples: str, lang: str) -> str:
    if lang == "zh":
        parts = [f"这段视频的口白内容：\n{transcript_text}"]
        if examples:
            parts.append(examples)
        parts.append("请根据以上内容写一条社交媒体帖文文案。")
    else:
        parts = [f"Transcript of this video's spoken content:\n{transcript_text}"]
        if examples:
            parts.append(examples)
        parts.append("Write a social media post caption based on the content above.")
    return "\n\n".join(parts)


def _call_llm_json(system_prompt: str, user_message: str, *, temperature: float) -> Optional[dict]:
    """call_llm_chat + json.loads，解析失败重试整次调用一遍——跟
    content_planner.py::_call_llm_json 同一个模式（同一个失败模式：调用本身
    成功但没吐出合法 JSON，重试一次经常就好了），本地重新实现一份而不是跨
    模块 import 私有函数——croll_script.py 对 content_planner.py 也是同样的
    独立处理，这个代码库里"卫星模块"之间不互相 reach into 对方的下划线开头
    helper 是既有约定。"""
    content = call_llm_chat(system_prompt, user_message, temperature=temperature)
    if content is None:
        logger.info("social_caption: 没配 LLM 或调用失败，跳过")
        return None
    try:
        return json.loads(content)
    except Exception as e:
        logger.warning(f"social_caption: 解析 LLM 输出失败，重试一次: {e}")

    content = call_llm_chat(system_prompt, user_message, temperature=temperature)
    if content is None:
        return None
    try:
        return json.loads(content)
    except Exception as e:
        logger.warning(f"social_caption: 重试后仍解析失败，放弃: {e}")
        return None


def _clean_hashtags(raw_tags: list) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        tag = tag.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag)
        if len(cleaned) >= _MAX_HASHTAGS:
            break
    return cleaned


def _normalize_caption_text(caption: str) -> str:
    """真人实测抓到的真实缺陷：模型偶尔在 JSON 字符串里把换行过度转义成字面上的
    反斜杠+n（`\\\\n`），json.loads 解析完还是两个字符的 "\\n"，不是真正的换行——
    交付出去用户会在消息里看到字面的 "\\n"。这里做一次机械纠正，不指望靠 prompt
    100% 杜绝这个模型输出层面的转义错误。"""
    return caption.replace("\\n", "\n")


# 简体字快速抽查——现在跑在 _to_traditional() 转换之后，正常情况下不应该再命中
# （s2t 覆盖了这几个字）。留着不是白留：万一 OpenCC 没装成功/初始化失败（见上面
# _S2T_CONVERTER = None 的兜底），这条 warning 就是唯一还剩的可观测信号，能看出
# 安全网是不是没生效，而不是静默放行简体字。
_SIMPLIFIED_SPOT_CHECK = ["个", "们", "说", "这", "来", "时", "没", "还", "从"]


def _lint_caption(caption: str, lang: str) -> None:
    blocklist = _BANNED_PHRASES_ZH if lang == "zh" else _BANNED_PHRASES_EN
    hits = [p for p in blocklist if p.lower() in caption.lower()]
    if hits:
        logger.warning(
            f"social_caption: 生成结果命中老套用语黑名单 {hits}——文案仍会交付，"
            "这条日志只是用来观察模型是否经常犯这个毛病，不拦截、不重跑"
        )
    if lang == "zh":
        simp_hits = [c for c in _SIMPLIFIED_SPOT_CHECK if c in caption]
        if simp_hits:
            logger.warning(
                f"social_caption: 生成结果可能混入简体字 {simp_hits}（只用繁體字是硬性要求，"
                "抽查非穷尽）——文案仍会交付，仅记录用于观察模型是否经常犯这个毛病"
            )
    # 破折号——客户明确反馈"KOL 真人打字基本不会用破折号"，只做机械检测不做
    # 自动替换：把 "—" 换成句号/逗号还要连带处理后面字母大小写才不会读起来
    # 断句奇怪，这种"聪明"替换比不替换更容易出错，所以跟老套用语一样只记录、
    # 不改写、不拦截，靠 prompt 侧的硬性要求 + 这条可观测信号来回归观察。
    if "—" in caption:
        logger.warning(
            "social_caption: 生成结果含有破折号「—」——用户明确要求过避免，"
            "文案仍会交付，仅记录用于观察模型是否经常犯这个毛病"
        )


def _generate_once(transcript_text: str, lang: str) -> Optional[dict]:
    system_prompt = _SYSTEM_ZH if lang == "zh" else _SYSTEM_EN
    examples = _format_examples(_pick_examples(lang), lang)
    user_message = _build_user_message(transcript_text, examples, lang)

    raw = _call_llm_json(system_prompt, user_message, temperature=_TEMPERATURE)
    if not raw:
        return None

    caption = _normalize_caption_text((raw.get("caption") or "").strip())
    if not caption:
        logger.warning("social_caption: LLM 返回空文案，视为失败")
        return None
    hashtags = _clean_hashtags(raw.get("hashtags") or [])

    if lang == "zh":
        caption = _to_traditional(caption)
        hashtags = [_to_traditional(h) for h in hashtags]

    _lint_caption(caption, lang)
    return {"lang": lang, "caption": caption, "hashtags": hashtags}


def generate_caption(job_dir: Path, edit_request: Optional[str]) -> Optional[dict]:
    """看这个 job 的转写文本，生成一条帖文文案 + hashtag。

    返回 {"lang": "zh"|"en", "caption": str, "hashtags": [str, ...]}；
    没有转写、LLM 不可用、两次都解析失败、或超过内部硬性耗时上限，一律返回
    None（不抛异常）——调用方应把这当"这步没成"处理，绝不能拿 None 编一段
    文案糊弄用户（跟 croll_script.write_script() 的失败约定一致）。"""
    segments = _read_transcript_segments(job_dir)
    if not segments:
        logger.info("social_caption: 没有转写文本（静音/纯音乐视频等正常情况），跳过文案生成")
        return None

    lang = _resolve_lang(edit_request)
    transcript_text = _build_transcript_text(segments)

    try:
        future = _executor.submit(_generate_once, transcript_text, lang)
        return future.result(timeout=_HARD_DEADLINE_S)
    except concurrent.futures.TimeoutError:
        logger.warning(f"social_caption: 生成超过 {_HARD_DEADLINE_S}s 硬性上限，放弃（不影响预览交付）")
        return None
    except Exception as e:
        logger.warning(f"social_caption: 生成失败: {e}")
        return None
