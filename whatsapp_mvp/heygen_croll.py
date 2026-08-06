"""C-roll 生成：静态照片 -> HeyGen 数字人说话视频。仿 pixabay_bg_music.py /
gemini_broll.py 的薄封装模式——纯 HTTP 调用，不依赖 tools/ 里那个只转发
VEO/Sora/Kling 的 heygen_video.py（那个没接真正的 Avatar/Talking Photo 接口，
2026-07-16 实测确认过）。

三步：上传照片拿 talking_photo_id -> 提交生成(文案+语音)拿 video_id -> 轮询
下载成品。失败一律返回 None/False，不抛异常——调用方按"这步没成"处理。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_UPLOAD_URL = "https://upload.heygen.com/v1/talking_photo"
_GENERATE_URL = "https://api.heygen.com/v2/video/generate"
_STATUS_URL = "https://api.heygen.com/v1/video_status.get"

# 标准档约 $1/分钟（HeyGen 官方定价，2026-07 查证）。生成完按实际时长算，
# 不信请求里的时长参数（同 gemini_broll 的做法——请求给的只是意图，不是事实）。
_COST_PER_SECOND = 1.0 / 60

# 按语言给个能直接用的默认声音（2026-07-16 实测过、口型自然、非机械音）。
# 以后要做成用户可选，这里先给个务实的默认，不为一个还没人用过的选项
# 提前搭一套配置界面。
_DEFAULT_VOICE_ID = {
    "zh": "961546a1be64458caa1386ff63dd5d5f",  # Yunyang - Professional
    "en": "e1a429dbe823406dbae5fa7c3612314d",  # Byron - Professional
}


def _api_key() -> Optional[str]:
    return os.environ.get("HEYGEN_API_KEY")


def _http_error_detail(e: Exception) -> str:
    """跟 pipeline_runner.py 的 ElevenLabs 401 修复同一个教训（Rule 10）：
    resp.raise_for_status() 抛出的异常字符串只有泛泛的状态行（"400 Client
    Error: BAD REQUEST for url: ..."），真正有用的原因在响应体里，
    raise_for_status() 从不读它。这里优先读 body 的 message/error 字段，
    读不到才退回原始异常字符串。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = resp.json()
            msg = body.get("message") or (body.get("error") or {}).get("message")
            if msg:
                return f"{msg} (HTTP {resp.status_code})"
        except ValueError:
            pass
    return str(e)


def is_available() -> bool:
    return bool(_api_key())


def upload_talking_photo(image_path: Path) -> Optional[str]:
    """上传照片，成功返回 talking_photo_id。"""
    key = _api_key()
    if not key:
        logger.warning("heygen_croll: 未配置 HEYGEN_API_KEY")
        return None
    ext = image_path.suffix.lower().lstrip(".")
    content_type = "image/png" if ext == "png" else "image/jpeg"
    try:
        data = image_path.read_bytes()
        r = requests.post(
            _UPLOAD_URL,
            headers={"x-api-key": key, "Content-Type": content_type},
            data=data, timeout=30,
        )
        r.raise_for_status()
        return r.json()["data"]["talking_photo_id"]
    except Exception as e:
        logger.warning(f"heygen_croll: 照片上传失败: {_http_error_detail(e)}")
        return None


def generate_talking_video(talking_photo_id: str, script: Optional[str] = None, lang: str = "zh",
                           aspect: str = "9:16", voice_id: Optional[str] = None,
                           audio_url: Optional[str] = None) -> Optional[str]:
    """提交生成请求，成功返回 video_id（异步任务，还要轮询）。

    默认走 HeyGen 自己的 TTS（voice.type=text，库存声音，script 必填）。传
    audio_url 时切到音频对口型模式（voice.type=audio）——HeyGen 不再合成语音，
    直接把照片对口型对齐到这段音频上，script 被忽略。这条路径是给
    voice_clone.py 用的：script 先用 ElevenLabs 克隆音色合成好、传一个公网可
    访问的 URL 过来（HeyGen 服务端自己去抓这个 URL，不是我们上传文件），这样
    出来的视频用的是艺人真实克隆的声音，不是 HeyGen 库存声音。
    """
    key = _api_key()
    if not key:
        return None
    w, h = (720, 1280) if aspect == "9:16" else (1280, 720)
    if audio_url:
        voice_payload = {"type": "audio", "audio_url": audio_url}
    else:
        resolved_voice = voice_id or _DEFAULT_VOICE_ID.get(lang, _DEFAULT_VOICE_ID["en"])
        voice_payload = {"type": "text", "input_text": script, "voice_id": resolved_voice}
    body = {
        "video_inputs": [{
            "character": {
                "type": "talking_photo",
                "talking_photo_id": talking_photo_id,
                "scale": 1.0,
            },
            "voice": voice_payload,
        }],
        "dimension": {"width": w, "height": h},
    }
    try:
        r = requests.post(
            _GENERATE_URL,
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        r.raise_for_status()
        return r.json()["data"]["video_id"]
    except Exception as e:
        logger.warning(f"heygen_croll: 提交生成失败: {_http_error_detail(e)}")
        return None


def poll_and_download(video_id: str, out_path: Path, timeout_s: int = 300,
                      poll_interval_s: int = 6) -> bool:
    """轮询直到成功/失败/超时；成功则把成品下载到 out_path。"""
    key = _api_key()
    if not key:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(_STATUS_URL, headers={"x-api-key": key},
                             params={"video_id": video_id}, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or {}
            status = data.get("status")
            if status == "completed":
                video_url = data.get("video_url")
                if not video_url:
                    logger.warning("heygen_croll: 状态 completed 但没有 video_url")
                    return False
                return _download(video_url, out_path)
            if status == "failed":
                logger.warning(f"heygen_croll: 生成失败: {data.get('error')}")
                return False
        except Exception as e:
            logger.warning(f"heygen_croll: 轮询异常（继续重试）: {e}")
        time.sleep(poll_interval_s)
    logger.warning(f"heygen_croll: 轮询超时({timeout_s}s)，video_id={video_id}")
    return False


def _download(url: str, out_path: Path) -> bool:
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, timeout=120, stream=True) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"heygen_croll: 成片下载失败: {e}")
        return False


def delete_talking_photo(talking_photo_id: str) -> bool:
    """用完即删，释放 HeyGen 账号的 photo avatar 配额（标准档只给 3 个，见
    2026-07-17 实测：`DELETE /v1/talking_photo/{id}` 返回 200 但不释放配额，
    配额单位其实是 avatar group——同一张照片上传后 group id 就等于
    talking_photo_id，删 group 才是真删除。失败只记警告不抛异常：清理失败不该
    把一次已经成功的生成变成失败任务。（合并自 PR #39，2026-07-20）"""
    key = _api_key()
    if not key:
        return False
    try:
        r = requests.delete(
            f"https://api.heygen.com/v2/avatar_group/{talking_photo_id}",
            headers={"x-api-key": key}, timeout=15,
        )
        r.raise_for_status()
        logger.info(f"heygen_croll: 已清理 talking photo {talking_photo_id}，释放配额")
        return True
    except Exception as e:
        logger.warning(f"heygen_croll: 清理 talking photo {talking_photo_id} 失败（不影响本次任务）: {_http_error_detail(e)}")
        return False


def probe_duration_seconds(path: Path) -> float:
    """成本按实际生成时长算，不信请求参数。"""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def estimate_cost(duration_seconds: float) -> float:
    return round(duration_seconds * _COST_PER_SECOND, 4)
