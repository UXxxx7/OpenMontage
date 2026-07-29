# gemini_broll.py 额度状态检测/缓存回归测试（架构复审后新增，2026-07-29）。
#
# 真实事故：job_9c671249eb76 实测复现——配置了 GOOGLE_API_KEY，但账号在免费
# 档，generativelanguage.googleapis.com 对 gemini-omni-flash 的免费档配额是
# 0（不是用完了，是免费档压根不分配额度）。用户直到确认之后、生成失败了
# 才知道这段 b-roll 没戏。这里加的是"失败一次就记住，后续确认消息提前
# 警示"这层——不产生真实调用，纯本地缓存文件读写。
#
# Run: uv run python -m pytest whatsapp_mvp/test_gemini_broll.py

from __future__ import annotations

import json
import time

import pytest

import whatsapp_mvp.gemini_broll as gemini_broll

# job_9c671249eb76 真实撞到的错误文本，原样摘录。
REAL_QUOTA_ERROR = (
    'Gemini Omni interaction failed (429): {"error":{"message":"You exceeded your '
    'current quota, please check your plan and billing details. For more information '
    'on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To '
    'monitor your current usage, head to: https://ai.dev/rate-limit. \\n* Quota exceeded '
    'for metric: generativelanguage.googleapis.com/generate_content_free_tier_input_'
    'token_count, limit: 0, model: gemini-omni-flash\\n* Quota exceeded for metric: '
    'generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, '
    'model: gemini-omni-flash\\nPlease retry in 32.722793022s.","code":"too_many_requests"}}'
)


@pytest.fixture(autouse=True)
def _isolated_cache_file(tmp_path, monkeypatch):
    """缓存文件走临时目录，不碰本机真实的 storage/_broll_generation_status.json，
    也不用管上一条测试有没有清理干净。"""
    fake_path = tmp_path / "_broll_generation_status.json"
    monkeypatch.setattr(gemini_broll, "_quota_status_path", lambda: fake_path)
    return fake_path


def test_is_quota_error_recognizes_the_real_incident_text():
    assert gemini_broll._is_quota_error(REAL_QUOTA_ERROR) is True


def test_is_quota_error_does_not_flag_unrelated_failures():
    """不能矫枉过正——提示词格式错、网络连接失败这类不是额度问题，不该被
    当成"账号级不可用"缓存起来，不然一次偶发的网络抖动就会误伤后面一整
    小时的确认消息。"""
    assert gemini_broll._is_quota_error("Connection refused") is False
    assert gemini_broll._is_quota_error("Invalid prompt: empty string") is False
    assert gemini_broll._is_quota_error("") is False


def test_availability_defaults_to_true_with_no_cached_failure(_isolated_cache_file):
    available, reason = gemini_broll.check_broll_generation_availability()
    assert available is True
    assert reason == ""


def test_availability_reflects_a_recorded_quota_failure(_isolated_cache_file):
    gemini_broll._record_quota_failure(REAL_QUOTA_ERROR)
    available, reason = gemini_broll.check_broll_generation_availability()
    assert available is False
    assert "429" in reason or "quota" in reason.lower() or "Quota" in reason


def test_availability_recovers_after_ttl_expires(_isolated_cache_file):
    gemini_broll._record_quota_failure(REAL_QUOTA_ERROR)
    # 手动把记录时间改到 TTL 之前，模拟"已经过了缓存有效期"
    data = json.loads(_isolated_cache_file.read_text())
    data["checked_at"] = time.time() - gemini_broll._QUOTA_CACHE_TTL_S - 10
    _isolated_cache_file.write_text(json.dumps(data))

    available, reason = gemini_broll.check_broll_generation_availability()
    assert available is True
    assert reason == ""


def test_availability_stays_unavailable_within_ttl(_isolated_cache_file):
    gemini_broll._record_quota_failure(REAL_QUOTA_ERROR)
    data = json.loads(_isolated_cache_file.read_text())
    data["checked_at"] = time.time() - (gemini_broll._QUOTA_CACHE_TTL_S / 2)
    _isolated_cache_file.write_text(json.dumps(data))

    available, _ = gemini_broll.check_broll_generation_availability()
    assert available is False


def test_generate_broll_records_quota_failure_on_429_response(monkeypatch, tmp_path, _isolated_cache_file):
    """端到端验证 generate_broll 自己会在真的撞上额度错误时写缓存——不是
    只有 check_broll_generation_availability 这半边测过就行。"""
    from tools.base_tool import ToolStatus, ToolResult

    class FakeTool:
        def get_status(self):
            return ToolStatus.AVAILABLE

        def execute(self, inputs):
            return ToolResult(success=False, error=REAL_QUOTA_ERROR)

    monkeypatch.setattr("tools.video.gemini_omni_video.GeminiOmniVideo", FakeTool)

    result = gemini_broll.generate_broll("a cat sitting on a desk", tmp_path / "out.mp4")
    assert result is None  # 失败优雅返回 None，不抛异常

    available, reason = gemini_broll.check_broll_generation_availability()
    assert available is False
    assert reason  # 记录了具体原因，不是空字符串


def test_generate_broll_clears_cached_failure_on_real_success(monkeypatch, tmp_path, _isolated_cache_file):
    """如果之前撞过额度失败、缓存记了"不可用"，但这次真的成功了（比如
    账号升级了套餐）——缓存应该被清掉，不能让一条过期记录继续误导后面
    的确认消息。"""
    gemini_broll._record_quota_failure(REAL_QUOTA_ERROR)
    available_before, _ = gemini_broll.check_broll_generation_availability()
    assert available_before is False

    from tools.base_tool import ToolStatus, ToolResult

    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(b"fake mp4 bytes")  # _probe_seconds 会 ffprobe 这个路径，允许它失败返回 0

    class FakeTool:
        def get_status(self):
            return ToolStatus.AVAILABLE

        def execute(self, inputs):
            return ToolResult(success=True, data={"interaction_id": "abc123"}, cost_usd=0.4)

    monkeypatch.setattr("tools.video.gemini_omni_video.GeminiOmniVideo", FakeTool)

    result = gemini_broll.generate_broll("a cat sitting on a desk", out_path)
    assert result is not None
    assert result["path"] == str(out_path)

    available, reason = gemini_broll.check_broll_generation_availability()
    assert available is True
    assert reason == ""
