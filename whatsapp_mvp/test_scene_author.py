# authored/scene_author.py 传输层加固回归测试（架构复审后新增，2026-07-29）。
#
# 真实发现：_default_llm_call 原来自己重写了一份 requests.post(timeout=240)，
# 跟同一晚在 llm_client.py 里整晚在修的 DeepSeek 传输层是完全独立的第二套
# 实现——没有硬性总耗时上限（服务器间歇挤字节时 timeout=240 一样防不住，
# 跟 llm_client 改之前一模一样的漏洞，DeepSeek 那条路上真实卡过 17 分钟），
# 也没有对连接中断类错误做任何重试（_invoke 原来只认 429/限流可重试）。
# 这里验证两处修复：(1) 复用 llm_client._post_bounded 拿到硬性总耗时上限，
# 且传了一个专属这类"现写场景代码"更重调用的更大上限（240s，不是 llm_
# client 默认给 DeepSeek JSON 规划调的 75s）；(2) _invoke 现在也会对传输层
# 瞬时故障（连接中断/超时/硬上限触发）做退避重试，不再只认限流。
#
# Run: uv run python -m pytest whatsapp_mvp/test_scene_author.py

from __future__ import annotations

import concurrent.futures
import time
import unittest.mock as mock

import pytest
import requests

import whatsapp_mvp.authored.scene_author as scene_author
import whatsapp_mvp.llm_client as llm_client


@pytest.fixture(autouse=True)
def _author_llm_env(monkeypatch):
    """给 _default_llm_call 一套假的 AUTHOR_LLM_* 配置，不依赖本机真实
    .env，也不会真的打网络。"""
    monkeypatch.setenv("AUTHOR_LLM_BASE_URL", "https://fake-author-llm.invalid")
    monkeypatch.setenv("AUTHOR_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AUTHOR_LLM_MODEL", "fake-model")


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """429/网络重试之间的退避默认是 5/15/30 秒——测试里不用真的等。"""
    monkeypatch.setenv("AUTHOR_LLM_RETRY_BACKOFF", "0,0,0")


def _fake_response(content: str, usage: dict | None = None):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}], "usage": usage or {}}

    return FakeResp()


def test_default_llm_call_uses_hardened_transport_and_a_wider_deadline_for_heavier_calls():
    """_default_llm_call 现在应该走 llm_client._post_bounded，而且传的硬
    上限是给这类更重调用用的 240s，不是 llm_client 给 DeepSeek JSON 规划
    调的默认 75s——直接检查调用参数，不用真的等一个卡住的连接。"""
    with mock.patch("whatsapp_mvp.llm_client._post_bounded") as fake_post_bounded:
        fake_post_bounded.return_value = _fake_response("hello", {"prompt_tokens": 1})
        result = scene_author._default_llm_call([{"role": "user", "content": "hi"}], 100, 0.2)

    assert result == {"content": "hello", "usage": {"prompt_tokens": 1}}
    assert fake_post_bounded.called
    kwargs = fake_post_bounded.call_args.kwargs
    assert kwargs.get("hard_deadline_s") == 240, (
        "应该显式传 240s 硬上限（现写场景代码的合理调用时长），不是 llm_client 的默认值"
    )


def test_default_llm_call_builds_correct_endpoint_for_a_versioned_base_url(monkeypatch):
    """真实复现的 bug（2026-07-29，直接拿本机真实配置的 VISION_LLM_BASE_URL
    调用 _default_llm_call 发现的，不是猜的）：base 是
    "https://open.bigmodel.cn/api/paas/v4"（已经带了自己的版本号，不以
    "/v1" 结尾）——原来的逻辑"不以 /v1 结尾就插一段 /v1/chat/completions"
    拼出 ".../v4/v1/chat/completions"，智谱这个网关下根本没有这条路径，
    直接 404。正确结果应该是 ".../v4/chat/completions"，不插那段 /v1
    ——跟 llm_client.call_vision_chat 对同一个 base 的拼法完全一致（那条
    路径整晚在真实调用，确认工作正常）。"""
    monkeypatch.setenv("AUTHOR_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    with mock.patch("whatsapp_mvp.llm_client._post_bounded") as fake_post_bounded:
        fake_post_bounded.return_value = _fake_response("hi")
        scene_author._default_llm_call([{"role": "user", "content": "hi"}], 100, 0.2)

    called_url = fake_post_bounded.call_args.args[0]
    assert called_url == "https://open.bigmodel.cn/api/paas/v4/chat/completions", called_url


def test_default_llm_call_actually_passes_its_deadline_through_to_post_bounded():
    """硬上限机制本身（后台线程 + future.result(timeout=...)）已经在
    test_llm_client.py 里用真正卡住的连接直接验证过——这里只需要确认
    _default_llm_call 把 hard_deadline_s=240 真的传到了 _post_bounded 里，
    而不是不小心漏传、悄悄退回 llm_client 自己的默认值（75s，是给不同
    工作量调的，见上面的模块注释）。用 _post_bounded 真实的参数签名调用一次
    （mock 掉最终发请求的 _session.post，不走真实网络），确认硬上限确实是
    240，不是 75。"""
    def slow_post(*a, **k):
        time.sleep(0.05)
        return _fake_response("ok")

    with mock.patch.object(llm_client._session, "post", side_effect=slow_post):
        result = scene_author._default_llm_call([{"role": "user", "content": "hi"}], 100, 0.2)
    assert result == {"content": "ok", "usage": {}}


def test_invoke_retries_transient_network_errors_not_just_rate_limits():
    """核心修复验证：_invoke 原来只对限流异常重试，连接中断/超时这类传输
    层瞬时故障会被当成"非限流异常"立即抛出、完全不重试。现在应该跟限流
    走同一套退避重试。"""
    call_count = {"n": 0}

    def flaky_call(messages, max_tokens, temperature):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise requests.ConnectionError("Response ended prematurely")
        return {"content": "ok after retries", "usage": {}}

    result = scene_author._invoke(flaky_call, [], 100, 0.2)
    assert result == {"content": "ok after retries", "usage": {}}
    assert call_count["n"] == 3, "应该重试到第 3 次才成功，前两次连接中断都被正确重试了"


def test_invoke_retries_hard_deadline_timeout_as_transient():
    """_post_bounded 的硬上限触发时抛的是 concurrent.futures.TimeoutError——
    确认这个具体异常类型也被 _invoke 识别为可重试。"""
    call_count = {"n": 0}

    def flaky_call(messages, max_tokens, temperature):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise concurrent.futures.TimeoutError("hard deadline exceeded")
        return {"content": "ok", "usage": {}}

    result = scene_author._invoke(flaky_call, [], 100, 0.2)
    assert result == {"content": "ok", "usage": {}}
    assert call_count["n"] == 2


def test_invoke_still_retries_rate_limit_errors_unchanged():
    """既有行为不能被破坏：429/限流的重试逻辑原样保留。"""
    call_count = {"n": 0}

    def flaky_call(messages, max_tokens, temperature):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise RuntimeError("429 Too Many Requests")
        return {"content": "ok", "usage": {}}

    result = scene_author._invoke(flaky_call, [], 100, 0.2)
    assert result == {"content": "ok", "usage": {}}
    assert call_count["n"] == 2


def test_invoke_does_not_retry_genuine_non_transient_errors():
    """不能矫枉过正：请求体格式错/鉴权失败这类真正的调用方问题，不属于
    限流也不属于传输层瞬时故障，必须立即抛出，不能被误当成"值得重试"。"""
    call_count = {"n": 0}

    def bad_request_call(messages, max_tokens, temperature):
        call_count["n"] += 1
        raise RuntimeError("400 Bad Request: invalid messages format")

    with pytest.raises(RuntimeError, match="400 Bad Request"):
        scene_author._invoke(bad_request_call, [], 100, 0.2)
    assert call_count["n"] == 1, "非限流、非网络错误应该立即抛出，不重试"


def test_invoke_raises_last_error_after_exhausting_retries_on_persistent_network_failure():
    """退避用尽仍然失败（持续性网络故障，不是一次性抖动）——应该抛出最后
    一次的异常，不是吞掉或无限重试。"""
    call_count = {"n": 0}

    def always_broken(messages, max_tokens, temperature):
        call_count["n"] += 1
        raise requests.Timeout("persistent timeout")

    with pytest.raises(requests.Timeout):
        scene_author._invoke(always_broken, [], 100, 0.2)
    # AUTHOR_LLM_RETRY_BACKOFF="0,0,0" → 3 次退避机会 + 最初 1 次 = 4 次总尝试
    assert call_count["n"] == 4
