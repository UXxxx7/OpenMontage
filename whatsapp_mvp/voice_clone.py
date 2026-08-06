"""艺人声音克隆：一次性用一小段语音注册出专属音色（ElevenLabs Instant Voice
Clone），之后每次 C-roll/social batch 生成都用这个音色合成语音，喂给 HeyGen
的音频对口型模式（voice.type=audio），而不是 HeyGen 自己的库存声音。

用 Instant Voice Clone（IVC）不用 Professional Voice Clone（PVC）——查过
.agents/skills/elevenlabs/reference.md，PVC 要 30 分钟起、3-6 小时训练、
还要走 captcha 语音验证流程，跟"后台随手录一段发过来"这个场景完全对不上。
IVC 只要几十秒到几分钟样本，几秒钟就能拿到能用的 voice_id，牺牲的音色保真度
对这个场景可以接受。这个项目自己的 elevenlabs 技能文档没收录 IVC，接口是
ElevenLabs 公开文档里 `POST /v1/voices/add` 这个基础接口，这里直接实现。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_ADD_VOICE_URL = "https://api.elevenlabs.io/v1/voices/add"


def _api_key() -> Optional[str]:
    return os.environ.get("ELEVENLABS_API_KEY")


def is_available() -> bool:
    return bool(_api_key())


def create_instant_voice_clone(audio_path: Path, name: str) -> Optional[str]:
    """用一段语音样本建一个 Instant Voice Clone，成功返回 voice_id。
    失败返回 None（调用方按"这步没成"处理，不阻断整条 onboarding 流程——
    大不了这次没建成克隆，后续生成继续走 HeyGen 库存声音兜底）。
    """
    key = _api_key()
    if not key:
        logger.warning("voice_clone: 未配置 ELEVENLABS_API_KEY")
        return None
    try:
        with open(audio_path, "rb") as f:
            files = {"files": (audio_path.name, f, "audio/mpeg")}
            data = {"name": name}
            r = requests.post(
                _ADD_VOICE_URL,
                headers={"xi-api-key": key},
                data=data, files=files, timeout=60,
            )
        r.raise_for_status()
        voice_id = r.json().get("voice_id")
        if not voice_id:
            logger.warning(f"voice_clone: 建克隆成功但响应里没有 voice_id: {r.text[:200]}")
            return None
        logger.info(f"voice_clone: 建好克隆 {name} -> {voice_id}")
        return voice_id
    except Exception as e:
        logger.warning(f"voice_clone: 建克隆失败: {e}")
        return None


def delete_voice(voice_id: str) -> bool:
    """删掉一个克隆音色（目前没有自动调用点——克隆音色是长期复用的资源，不像
    heygen_croll 的 talking_photo 那样用完即删。留着给"艺人要求重新录一次
    覆盖旧克隆"这类以后的场景用，同一套"失败只警告不抛异常"的纪律。"""
    key = _api_key()
    if not key:
        return False
    try:
        r = requests.delete(f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                            headers={"xi-api-key": key}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"voice_clone: 删除 {voice_id} 失败: {e}")
        return False


def synthesize_for_heygen(script: str, elevenlabs_voice_id: Optional[str], job_dir: Path) -> Optional[str]:
    """如果这个用户有克隆音色，用它合成语音、落进 job_dir、返回这段音频的公网
    URL——HeyGen 靠这个 URL 自己去抓音频（见 heygen_croll.generate_talking_video
    的 audio_url 参数），不是我们直接上传文件给它。没有克隆音色、或合成失败，
    返回 None，调用方据此决定退回 HeyGen 库存声音（script/voice_id 模式）。

    音频文件直接放进 job_dir、复用现成的 /files/{job_id}/{filename} 路由对外
    暴露——不用另起一套文件托管，job_dir 本来就是公网可达的（webhook.py 的
    serve_file，2026-08 加了 .mp3 的 media_type 支持）。
    """
    if not elevenlabs_voice_id:
        return None
    audio_path = job_dir / "_voice_clone_audio.mp3"
    if not synthesize_speech(script, elevenlabs_voice_id, audio_path):
        return None
    from .config import get_config
    config = get_config()
    base = config.public_base_url.rstrip("/")
    job_id = job_dir.name  # database.py 的 Job.job_dir 就是 jobs_dir/job.id
    return f"{base}/files/{job_id}/{audio_path.name}"


def synthesize_speech(text: str, voice_id: str, out_path: Path) -> bool:
    """用克隆音色把文案合成语音，落盘到 out_path。复用 tools/audio/elevenlabs_tts.py
    这个既有工具（不重新拼一遍 HTTP 请求），因为它已经处理好了 voice_settings/
    output_format 这些细节。成功返回 True，失败返回 False。"""
    from tools.audio.elevenlabs_tts import ElevenLabsTTS
    from tools.base_tool import ToolStatus

    tool = ElevenLabsTTS()
    if tool.get_status() != ToolStatus.AVAILABLE:
        logger.warning("voice_clone: ElevenLabsTTS 不可用")
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = tool.execute({
            "text": text,
            "voice_id": voice_id,
            "output_path": str(out_path),
            # multilingual_v2 中英文都覆盖，不用按 lang 切模型。
            "model_id": "eleven_multilingual_v2",
        })
        if not result.success:
            logger.warning(f"voice_clone: 语音合成失败: {result.error}")
            return False
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"voice_clone: 语音合成异常: {e}")
        return False
