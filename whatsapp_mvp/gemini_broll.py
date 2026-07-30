"""WhatsApp MVP 的 b-roll 生成封装：包一层 tools.video.gemini_omni_video。

从一段文字 prompt 生成一小段 b-roll，落到 out_path。
关键：**按真实成片秒数记成本**（Omni 自选 3-10s、无视 duration 提示），
生成后 ffprobe 一下 × $0.10，而不是拿 duration 估算自欺。
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_COST_PER_SECOND = 0.10  # $0.10/秒输出（5792 tokens/秒 × $17.5/1M）

# 真实事故（2026-07-29）：配置了 GOOGLE_API_KEY 但账号在免费档，
# generativelanguage.googleapis.com 对 gemini-omni-flash 的免费档配额是 0——
# 不是用光了，是免费档压根不给这个模型分配额度。GeminiOmniVideo.get_status()
# 只检查 key 有没有配置，查不出这种"配置了但用不了"的情况；这类账号级限制
# 也没有便宜的"查一下还有没有额度"接口，只能等一次真实调用失败了才知道。
# 与其每次都让用户干等一轮生成失败才知道，失败一次之后把这个信号缓存起来，
# 后续同一批规划/确认消息可以提前警示——真的到了升级套餐解决问题的那一刻，
# 缓存的 TTL 到期后会自动重新尝试，不会永久卡死。
_QUOTA_CACHE_TTL_S = 3600  # 1 小时——足够避免同一段时间内反复撞同一个已知失败，
                           # 又不会在用户升级套餐后卡太久才恢复


def _quota_status_path() -> Path:
    from .config import get_config
    return get_config().storage_root / "_broll_generation_status.json"


def _is_quota_error(error_text: str) -> bool:
    """判断失败原因是不是账号额度类问题（限流/配额耗尽/免费档不支持），
    而不是提示词、网络等其它原因——只有这类才值得缓存"当前不可用"。"""
    s = (error_text or "").lower()
    return any(kw in s for kw in (
        "quota", "429", "too_many_requests", "resource_exhausted", "rate limit",
    ))


def _record_quota_failure(reason: str) -> None:
    path = _quota_status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "available": False,
            "reason": reason[:300],
            "checked_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"gemini_broll: 写入额度状态缓存失败（不影响主流程）: {e}")


def check_broll_generation_availability() -> tuple[bool, str]:
    """AI 生成 b-roll 当前是否可用；不可用时给出人话原因，供确认消息提前
    警示用户（架构复审后新增，2026-07-29，真实事故驱动）。

    只做两件事，都不产生真实调用：(1) 有没有配置 API key；(2) 最近一次真实
    生成有没有撞上过额度类失败、还在缓存有效期内。两者都过了才算"可用"——
    但这只代表"最近没有已知会失败的理由"，不是保证这次一定能成功（真实
    额度状态只有一次真实调用才能确认）。
    """
    from tools.base_tool import ToolStatus
    from tools.video.gemini_omni_video import GeminiOmniVideo

    if GeminiOmniVideo().get_status() != ToolStatus.AVAILABLE:
        return False, "未配置 AI 生成 b-roll 所需的 API key（GEMINI_API_KEY/GOOGLE_API_KEY）"

    path = _quota_status_path()
    if not path.exists():
        return True, ""
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, ""
    if cached.get("available", True):
        return True, ""
    age = time.time() - float(cached.get("checked_at", 0))
    if age >= _QUOTA_CACHE_TTL_S:
        return True, ""  # 缓存过期，给它一次重新尝试的机会
    return False, cached.get("reason") or "AI 生成 b-roll 当前不可用（近期请求失败，可能是账号额度问题）"


def _probe_seconds(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def generate_broll(prompt: str, out_path, aspect: str = "9:16") -> Optional[dict]:
    """生成一段 b-roll。成功返回 {path, seconds, cost_usd, interaction_id}，失败返回 None。

    失败一律返回 None（不抛异常）——调用方（insert_broll）应把它当"这段 b-roll 没生成成"
    优雅跳过，不拖垮整条管线。
    """
    from tools.base_tool import ToolStatus
    from tools.video.gemini_omni_video import GeminiOmniVideo

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tool = GeminiOmniVideo()
    if tool.get_status() != ToolStatus.AVAILABLE:
        logger.warning("gemini_broll: provider 不可用（未配 GEMINI_API_KEY/GOOGLE_API_KEY，或非付费档）")
        return None

    # 用时间码前缀轻微引导往短里压（不保证——模型仍在 3-10s 自选）
    guided = prompt if prompt.strip().startswith("[") else f"[0-4s] {prompt}"

    try:
        result = tool.execute({
            "prompt": guided,
            "operation": "text_to_video",
            "aspect_ratio": aspect,
            "output_path": str(out_path),
        })
    except Exception as e:
        logger.warning(f"gemini_broll: 调用异常: {e}")
        if _is_quota_error(str(e)):
            _record_quota_failure(str(e))
        return None

    if not result.success:
        logger.warning(f"gemini_broll: 生成失败: {result.error}")
        if _is_quota_error(result.error or ""):
            _record_quota_failure(result.error or "")
        return None

    # 真实成功一次，说明额度问题（如果之前有缓存）已经不成立了——清掉缓存，
    # 不让一条过期的"不可用"记录在问题已经解决之后还继续误导确认消息。
    try:
        _quota_status_path().unlink(missing_ok=True)
    except OSError:
        pass

    seconds = _probe_seconds(out_path)
    cost = round(_COST_PER_SECOND * seconds, 3) if seconds else result.cost_usd
    logger.info(f"gemini_broll: 生成 {seconds:.1f}s → ${cost} @ {out_path}")
    return {
        "path": str(out_path),
        "seconds": seconds,
        "cost_usd": cost,
        "interaction_id": result.data.get("interaction_id"),
    }