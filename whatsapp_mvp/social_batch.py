"""社媒批次生成：一张照片 + 一句语音方向提示 -> 一批多平台内容（IG Feed /
Reel·TikTok / Story），每个变体各自的文案+hashtag，共享同一条 HeyGen 基础视频。

设计取舍——HeyGen 只生成一次，不是三次：
- Reel 和 Story 用同一条 HeyGen 说话视频（HeyGen 默认就出 9:16，两个平台
  宽高比一样，硬生成两次纯粹是重复花钱——$1/分钟不能按平台数乘）。
- Feed 用原始照片本身做静态图（中心裁切成方形），不额外生成视频——多数
  艺人的 IG Feed 帖子本来就是照片为主，硬凑一条视频进 Feed 反而不像真人发的。
- 三个变体各自的文案/hashtag 才是真正"按平台"生成的部分，风格规则来自
  "Backstage AI Conductor"这个产品设想里对每个平台语气的拆解。

跟 croll.py 的关系：复用同一套 heygen_croll（上传/生成/轮询/清理配额）+
croll_script.write_script（看图写口播文案），不复用 process_incoming_message
那条完整剪辑管线（转写/L2规划/apply_style 模板渲染）——MVP 阶段这批内容是
"发布就绪的素材"，不需要过那套面向单条长视频剪辑的复杂管线。
"""
from __future__ import annotations

import logging
import re as _re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from .llm_client import call_vision_chat

logger = logging.getLogger(__name__)

# 只用繁體字是硬性要求，但光靠 prompt 管不住（social_caption.py 那边真人实测
# 抓到过模型自己混入简体字）。这里加同一道机械防线：产出前统一过一遍简转繁。
# s2t（通用简转繁）而不是 s2hk（香港政府官方字形表）——两者对同一批字给出不同
# 字形（"説" vs "說"），s2t 给出的是日常打字实际会打出来的字形，更贴近这段文案
# 要被复制去发帖的真实使用场景。
try:
    from opencc import OpenCC
    _S2T_CONVERTER: Optional["OpenCC"] = OpenCC("s2t")
except Exception as e:
    logger.warning(f"social_batch: OpenCC 初始化失败，简转繁安全网关闭: {e}")
    _S2T_CONVERTER = None


def _to_traditional(text: str) -> str:
    if _S2T_CONVERTER is None:
        return text
    try:
        return _S2T_CONVERTER.convert(text)
    except Exception as e:
        logger.warning(f"social_batch: 简转繁调用失败，放行原文: {e}")
        return text

# 平台变体定义：asset_kind 决定这个变体用照片还是视频；order 决定生成顺序
# （video 类共享同一条基础视频，只需生成一次，放前面）。
PLATFORM_SPECS = [
    {"platform": "instagram_reel", "label_zh": "IG Reel / TikTok", "asset_kind": "video"},
    {"platform": "instagram_story", "label_zh": "IG Story", "asset_kind": "video"},
    {"platform": "instagram_feed", "label_zh": "IG Feed", "asset_kind": "photo"},
]

_CAPTION_RULES = {
    "instagram_feed": {
        "zh": "写一段充满氛围感的走心长文（3-5 句），配 3 个貼切的 emoji（穿插在文字里，不要全堆在结尾）。语气私人、像本人自己发的，不是通稿。",
        "en": "Write an atmospheric, heartfelt caption (3-5 sentences) with 3 well-placed emoji woven into the text (not all dumped at the end). Personal tone, like the poster wrote it themselves, not a press release.",
    },
    "instagram_reel": {
        "zh": "写一句能在前3秒抓住人的钩子文案（1-2 句，够短够冲），配合视频里的高光瞬间。可以加1个貼切的 emoji 加强语气，不必强求。",
        "en": "Write a scroll-stopping hook line (1-2 sentences, punchy) that works as the first 3 seconds of text on screen, matching the video's peak moment. One well-placed emoji is fine if it fits, don't force it.",
    },
    "instagram_story": {
        "zh": "写一句简短的互动文字（1 句话），像投票贴纸或提问贴纸配的文案——邀请粉丝回复或互动，不是陈述。可以加1个 emoji。",
        "en": "Write one short interactive line — like the text next to a poll or question sticker — inviting a reply, not a statement. One emoji is fine.",
    },
}
_HASHTAG_COUNT = {"instagram_feed": 0, "instagram_reel": 8, "instagram_story": 0}

# 老套/AI 腔調用語黑名單——跟 social_caption.py 的做法一致：一份清單，prompt 里當
# 硬性禁止項，生成後再拿來做機械檢查（見 _lint_caption），不指望只靠 prompt 就
# 100% 杜絕。
_BANNED_PHRASES_ZH = [
    "在這個瞬息萬變的時代", "在这个瞬息万变的时代",
    "你知唔知道", "你有無諗過", "你有沒有想過",
    "立即聯繫我了解更多", "立即联系我了解更多",
]
_BANNED_PHRASES_EN = [
    "in today's fast-paced world", "let's dive in", "have you ever wondered",
    "contact me today to learn more", "3 things you need to know",
]


def _base_photo_prompt(hint: str, platform: str, lang: str) -> str:
    rules = _CAPTION_RULES[platform][lang]
    hint_line = ""
    if hint.strip():
        hint_line = (f'方向提示："{hint.strip()}"，围绕这个来写。\n' if lang == "zh"
                    else f'Direction given: "{hint.strip()}" — write around it.\n')
    hashtag_n = _HASHTAG_COUNT[platform]
    if lang == "zh":
        hashtag_line = (f"文案写完后单独另起一行，只放 {hashtag_n} 个貼切的英文 hashtag（空格分隔，"
                        "带#号，不要混进文案正文里）。" if hashtag_n else "不要加任何 hashtag。")
        return (
            # 原本这里硬写死"这张照片来自一位艺人的巡演后台"——跟这个产品实际的目标
            # 用户（香港保险从业员/KOL）完全对不上，硬套这个场景只会让配文显得莫名其妙。
            # 交给照片本身 + hint 去带出真实场景，不预设身份。
            "这张照片准备发布到社交媒体，请看图写一段配文。\n"
            f"{hint_line}{rules}\n"
            "只用繁體字，不可以出现简体字（哪怕看图判断出来的内容让你想用简体，输出也必须是繁體）。\n"
            "唔好用破折號「—」，一次都唔好——用句號、逗號或者換行代替，真人用手機打字幾乎唔會打破折號。\n"
            "絕對唔好出現呢啲老套/AI 腔調嘅講法：「在這個瞬息萬變的時代」、連續堆疊反問句\n"
            "（「你有沒有想過…你知唔知道…」）、清單式開頭（「3個原因」）、推銷式收尾\n"
            "（「立即聯繫我了解更多」）、空洞嘅行業套話。\n"
            "文案入面提到嘅嘢要同相片本身或者方向提示對得上——唔好編造相片入面睇唔到、\n"
            "方向提示冇講過嘅內容。\n"
            "emoji 必须是真正的 emoji 字符（比如 🎤 ❤️ ✨），"
            "绝对不要用「[鼓掌]」「[心形]」这种方括号文字描述代替 emoji。\n"
            "只输出文案本身，不要解释、不要加引号、不要 markdown。\n"
            f"{hashtag_line}"
        )
    hashtag_line = (f"After the caption, on its own separate line, give exactly {hashtag_n} "
                    "relevant hashtags (space-separated, with #) — do not mix hashtags into "
                    "the caption text itself." if hashtag_n else "Do not include any hashtags.")
    return (
        # Previously hardcoded "this photo is from an artist's tour backstage" here — doesn't
        # match this product's actual target user (HK insurance agents/KOLs), and forcing that
        # scenario onto an unrelated photo just reads as bizarre. Let the photo + hint carry
        # the real context instead of presupposing one.
        "This photo is being posted to social media. Look at the photo and write the caption.\n"
        f"{hint_line}{rules}\n"
        "No em dashes (—), anywhere — use a period, comma, or line break instead; real "
        "people typing on a phone essentially never use them.\n"
        "Never use these clichés, not even once: \"In today's fast-paced world...\", \"Let's "
        "dive in\", stacked rhetorical questions (\"Have you ever wondered... Did you "
        "know...\"), listicle framing (\"3 reasons why...\"), corporate CTA closers (\"Contact "
        "us today to learn more\"), generic platitudes.\n"
        "Everything in the caption must match what's actually visible in the photo or stated "
        "in the direction above — don't invent details you can't see or weren't told.\n"
        "Emoji must be real emoji characters (e.g. 🎤 ❤️ ✨) — never bracketed text "
        "placeholders like \"[clap]\" or \"[heart]\".\n"
        "Output only the caption itself — no explanation, no quotes, no markdown.\n"
        f"{hashtag_line}"
    )


def generate_social_caption(photo_path: str, platform: str, lang: str = "zh",
                            hint: str = "") -> Optional[dict]:
    """看图写一条平台专属文案。成功返回 {"caption": str, "hashtags": list[str]}；
    LLM 不可用/无输出返回 None（调用方按"这条没成"处理，不硬造文案糊弄艺人）。
    """
    hint = hint or ""
    prompt = _base_photo_prompt(hint, platform, lang)
    result, _usage = call_vision_chat(prompt, [photo_path])
    if not result or not result.strip():
        logger.warning(f"social_batch: {platform} 文案生成失败（视觉 LLM 不可用或无输出）")
        return None

    # 用正则直接从全文抓 #token，不依赖模型老实把 hashtag 单独分行——实测
    # 中文输出会把 hashtag 混进文案同一行（"...音乐无界 #新歌首唱会 #舞台..."），
    # 之前按"整行是不是主要由 #token 组成"判断的行级启发式在这种情况下抓不到。
    hashtag_re = _re.compile(r"#([\w一-鿿]+)#?")
    hashtags = hashtag_re.findall(result)
    caption = hashtag_re.sub("", result).strip()
    # 清理掉抠掉 hashtag 后留下的连续空行/多余空格
    caption = _re.sub(r"[ \t]+\n", "\n", caption)
    caption = _re.sub(r"\n{3,}", "\n\n", caption).strip()
    if not caption:
        # 保守兜底：万一整段都被当成 hashtag 抠空了，用原始输出兜底，不返回空文案。
        caption = result.strip()

    if lang == "zh":
        caption = _to_traditional(caption)
        hashtags = [_to_traditional(h) for h in hashtags]

    _lint_caption(caption, lang)
    return {"caption": caption, "hashtags": hashtags}


def _lint_caption(caption: str, lang: str) -> None:
    """非阻断检查——命中只记 warning，不拦截、不重跑，跟 social_caption.py 的
    _lint_caption 同一个哲学：给开发者留观测信号，不指望 LLM 自我审查靠得住。"""
    blocklist = _BANNED_PHRASES_ZH if lang == "zh" else _BANNED_PHRASES_EN
    hits = [p for p in blocklist if p.lower() in caption.lower()]
    if hits:
        logger.warning(f"social_batch: 生成结果命中老套用语黑名单 {hits}——文案仍会交付")
    if "—" in caption:
        logger.warning("social_batch: 生成结果含有破折号「—」——文案仍会交付")


def generate_batch(batch_id: str, user_id: int, photo_path: str, lang: str = "zh",
                   hint: str = "") -> list[str]:
    """批次编排主入口：一张照片 -> 一条 HeyGen 基础视频（Reel/Story 共用）+
    一张方形静态图（Feed）-> 三个平台各自的文案。返回创建成功的 job_id 列表
    （失败的变体不返回，调用方按"这个批次里有几条就绪"处理，不整批失败）。
    """
    from . import heygen_croll
    from .croll_script import write_script
    from .job_manager import create_job, update_job_fields, update_job_status
    from .database import JobStatus, get_session, User
    from .pipeline_runner import _record_generation_cost
    from .voice_clone import synthesize_for_heygen

    created_job_ids: list[str] = []
    base_video_path: Optional[Path] = None
    talking_photo_id: Optional[str] = None

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        elevenlabs_voice_id = user.elevenlabs_voice_id if user else None
    finally:
        session.close()

    # 音频对口型模式（voice_clone.py）需要一个公网 URL 让 HeyGen 自己去抓，
    # 但这时候还没有任何 job/job_dir 可以落盘——提前把第一个"视频类"变体的
    # job 建出来当暂存位（复用现成的 /files/{job_id}/{filename} 路由），
    # 用完了它照常在下面的主循环里被当成一个正常变体收尾，不浪费。
    first_video_platform = next((s["platform"] for s in PLATFORM_SPECS if s["asset_kind"] == "video"), None)
    staging_job = None
    if first_video_platform:
        staging_job = create_job(user_id=user_id, pipeline="social-batch",
                                 input_caption=hint, batch_id=batch_id, platform=first_video_platform)
        staging_job.job_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 共享的 HeyGen 基础视频（Reel + Story 用同一条）──────────────
    try:
        if heygen_croll.is_available():
            script = write_script(photo_path, lang=lang, hint=hint)
            if script:
                talking_photo_id = heygen_croll.upload_talking_photo(Path(photo_path))
                if talking_photo_id:
                    audio_url = (synthesize_for_heygen(script, elevenlabs_voice_id, staging_job.job_dir)
                                if staging_job else None)
                    if audio_url:
                        logger.info(f"social_batch: 批次 {batch_id} 使用克隆音色语音")
                        video_id = heygen_croll.generate_talking_video(talking_photo_id, audio_url=audio_url)
                    else:
                        video_id = heygen_croll.generate_talking_video(talking_photo_id, script, lang=lang)
                    if video_id:
                        tmp_out = Path(f"/tmp/social_batch_{batch_id}.mp4")
                        if heygen_croll.poll_and_download(video_id, tmp_out, timeout_s=900):
                            base_video_path = tmp_out
        else:
            logger.warning("social_batch: HEYGEN_API_KEY 未配置，Reel/Story 变体将跳过，仅生成 Feed")
    except Exception as e:
        logger.exception(f"social_batch: 基础视频生成出错（不影响 Feed 变体）: {e}")
    finally:
        if talking_photo_id:
            heygen_croll.delete_talking_photo(talking_photo_id)  # 用完即删，同 croll 的配额纪律

    base_cost = None
    if base_video_path and base_video_path.exists():
        duration = heygen_croll.probe_duration_seconds(base_video_path)
        base_cost = heygen_croll.estimate_cost(duration)

    # ── 2. 逐平台变体：建 job + 落素材 + 生成文案 ──────────────────────
    for spec in PLATFORM_SPECS:
        platform = spec["platform"]
        needs_video = spec["asset_kind"] == "video"
        if needs_video and not (base_video_path and base_video_path.exists()):
            logger.warning(f"social_batch: 跳过 {platform}（基础视频没生成成功）")
            continue

        # 第一个视频类变体复用上面已经建好的暂存 job（省一次重复建 job），
        # 其余变体正常新建。
        if staging_job is not None and platform == first_video_platform:
            job = staging_job
            staging_job = None  # 只复用一次，避免后续同平台重复出现时误判
        else:
            job = create_job(user_id=user_id, pipeline="social-batch",
                             input_caption=hint, batch_id=batch_id, platform=platform)
            job.job_dir.mkdir(parents=True, exist_ok=True)

        if needs_video:
            final_path = job.job_dir / "final.mp4"
            shutil.copy(base_video_path, final_path)
            if base_cost:
                _record_generation_cost(job.job_dir, f"social_batch[{platform}]", base_cost / 2)
        else:
            final_path = job.job_dir / "final.jpg"
            _center_crop_square(Path(photo_path), final_path)

        caption_result = generate_social_caption(photo_path, platform, lang=lang, hint=hint)
        update_job_fields(
            job.id,
            final_path=str(final_path),
            social_caption=(caption_result or {}).get("caption") or "",
            social_hashtags=__import__("json").dumps((caption_result or {}).get("hashtags") or []),
        )
        update_job_status(job.id, JobStatus.DONE if caption_result else JobStatus.ERROR,
                          None if caption_result else "文案生成失败")
        created_job_ids.append(job.id)

    # staging_job 还没被 None 掉 = 基础视频生成失败，从没进过上面的收尾分支
    # （needs_video 分支在视频没生成成功时直接 continue 跳过了它）——不然
    # 这条 job 会永远卡在 RECEIVED 状态，变成孤儿任务。
    if staging_job is not None:
        update_job_status(staging_job.id, JobStatus.ERROR, "基础视频生成失败")

    if base_video_path and base_video_path.exists():
        base_video_path.unlink(missing_ok=True)

    return created_job_ids


def _center_crop_square(src: Path, out: Path) -> bool:
    """把照片中心裁切成 1:1（Feed 用）。不做人脸检测跟踪——静态图片的"大致居中"
    已经够用，犯不着为一张图片引入 auto_reframe.py 那套面向视频的人脸跟踪。"""
    import subprocess
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            logger.warning(f"social_batch: 方形裁切失败: {(r.stderr or '')[-200:]}")
            return False
        return out.exists() and out.stat().st_size > 0
    except Exception as e:
        logger.warning(f"social_batch: 方形裁切异常: {e}")
        return False
