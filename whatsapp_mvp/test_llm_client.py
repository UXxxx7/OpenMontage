# llm_client._post_bounded 硬性总耗时上限回归测试。
#
# 真实事故（2026-07-27~28）：requests 的 timeout= 只保证单次 socket 读取
# 不超时，服务器间歇性挤字节时计时器不断被重置，配置了 timeout=60 的调用
# 实测跑了 17 分钟才报错。历史日志里 65 次网络错误，仅 2026-07-27 一晚就
# 19 次。_post_bounded 用一个后台线程 + future.result(timeout=...) 包一层，
# 不管服务器怎么"挤牙膏"，到硬上限就不再等——这里验证的就是这个硬上限
# 真的生效，不依赖真实网络请求。
#
# Run: uv run python -m pytest whatsapp_mvp/test_llm_client.py

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

import whatsapp_mvp.llm_client as llm_client


@pytest.fixture(autouse=True)
def _fast_deadline_for_tests(monkeypatch):
    """把硬上限调小，测试跑起来快，也更容易在断言里量化"用真的等满了
    没有"。"""
    monkeypatch.setattr(llm_client, "_HARD_CALL_DEADLINE_S", 0.3)


def test_post_bounded_gives_up_before_a_slow_trickling_server_ever_responds():
    def slow_post(*a, **k):
        time.sleep(5)  # 远超硬上限，模拟服务器间歇挤字节导致的长时间卡顿
        raise AssertionError("不应该跑到这里——硬上限应该早就先触发了")

    calling_thread_blocked_for = {}

    def run():
        start = time.monotonic()
        try:
            llm_client._post_bounded("http://fake.invalid", {}, {}, timeout=60)
        except concurrent.futures.TimeoutError:
            calling_thread_blocked_for["elapsed"] = time.monotonic() - start

    import unittest.mock as mock
    with mock.patch.object(llm_client._session, "post", side_effect=slow_post):
        run()

    assert "elapsed" in calling_thread_blocked_for, "应该抛出 concurrent.futures.TimeoutError"
    # 硬上限是 0.3s；给点调度余量，但必须远小于模拟的 5s 卡顿
    assert calling_thread_blocked_for["elapsed"] < 2.0, calling_thread_blocked_for["elapsed"]


def test_post_bounded_returns_normally_when_request_finishes_within_deadline():
    class FakeResp:
        status_code = 200

    def fast_post(*a, **k):
        return FakeResp()

    import unittest.mock as mock
    with mock.patch.object(llm_client._session, "post", side_effect=fast_post):
        resp = llm_client._post_bounded("http://fake.invalid", {}, {}, timeout=60)
    assert resp.status_code == 200


def test_post_with_retries_treats_hard_deadline_timeout_as_a_retryable_network_error(monkeypatch):
    """_post_with_retries 已有的重试循环本来就吃 ConnectionError/Timeout/
    ChunkedEncodingError——硬上限触发的 concurrent.futures.TimeoutError 得
    走同一条路径，不能被当成未捕获异常一路往上抛，把调用方的重试预算
    完全绕过。

    注意：不能用 mock.patch 全局替换 time.sleep 来加速重试间的 backoff——
    下面模拟"卡住的请求"用的也是 time.sleep，两者共享同一个 time 模块，
    全局替换会连模拟的卡顿都一起消灭，测试就测不到硬上限了。改成只把
    backoff 基数调小。
    """
    monkeypatch.setattr(llm_client, "_RETRY_BACKOFF_BASE_SECONDS", 0.01)
    call_count = {"n": 0}

    def always_slow(*a, **k):
        call_count["n"] += 1
        time.sleep(5)

    import unittest.mock as mock
    with mock.patch.object(llm_client._session, "post", side_effect=always_slow):
        result = llm_client._post_with_retries("test", "http://fake.invalid", {}, {}, timeout=60)

    assert result is None  # 重试预算耗尽后优雅返回 None，不是异常往外抛
    assert call_count["n"] == llm_client._MAX_ATTEMPTS
