# POST /croll 的 b-roll 参数回归测试（架构复审后新增，2026-07-29）。
#
# 之前 /croll 只接收 photo/hint/lang/pipeline，没有 broll 相关参数——图生
# 视频（HeyGen 数字人）生成出的 job 上永远不会有 role=="broll" 的资产，
# insert_broll 因此永远不会被 L2 规划器 emit，不是管线跑不通，是这个入口
# 没给它素材可用。补齐跟 POST /jobs 完全同一套参数形状/登记逻辑后，这里
# 验证：(1) 传了 broll 时正确落盘+登记进 job.assets，(2) 不传 broll 时跟
# 改动前完全一样（向后兼容，覆盖 Node 网关现有的调用方式）。
#
# 不用 fastapi.testclient/httpx——CI 那台机器全新 resolve 依赖时装到的
# starlette 版本强制要求装 httpx2 包（本机 .venv 里缓存的是能work的旧组合，
# 本地跑没暴露，CI 上 collection 阶段直接 RuntimeError，整个套件跑不起来，
# 实测复现过）。File()/Form() 只是 FastAPI 路由层的依赖注入默认值标记，
# 直接当普通 Python 函数调用端点、自己构造 UploadFile，完全绕开真实 HTTP
# 请求解析这层，不需要 TestClient，也就不需要 httpx。
#
# 不真的触发 HeyGen（要花钱、要等几分钟）——用 mock 顶掉 _run_in_background，
# 只验证端点自己同步做的落盘/登记那部分。
#
# Run: uv run python -m pytest whatsapp_mvp/test_webhook.py

from __future__ import annotations

import asyncio
import io
import unittest.mock as mock
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

import whatsapp_mvp.webhook as webhook
from whatsapp_mvp.job_manager import get_assets, get_job, get_session
from whatsapp_mvp.database import Job


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=filename)


@pytest.fixture
def cleanup_jobs():
    """测试期间创建的 job 会真的落盘 + 写数据库——用完清理掉，不留测试
    垃圾在本地存储/数据库里。"""
    created: list[str] = []
    yield created
    for job_id in created:
        job = get_job(job_id)
        if job is not None:
            import shutil
            shutil.rmtree(job.job_dir, ignore_errors=True)
        session = get_session()
        try:
            session.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()


def test_croll_registers_broll_assets(cleanup_jobs):
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        resp = asyncio.run(webhook.create_croll_endpoint(
            photo=_upload("photo.jpg", b"fake-jpeg"),
            hint="test hint",
            lang="en",
            pipeline="talking-head",
            broll=[_upload("clip1.mp4", b"fake-mp4-bytes"), _upload("clip2.png", b"fake-png-bytes")],
            broll_labels=["office shot", "logo closeup"],
            broll_kinds=["", ""],
            wa_number="api_user",
        ))

    job_id = resp["job_id"]
    cleanup_jobs.append(job_id)
    assert resp["status"] == "RECEIVED"

    # generate_croll 的调用签名不受这次改动影响——broll 处理完全是端点自己
    # 同步做的，跟后台生成流程解耦。
    assert fake_bg.called
    args = fake_bg.call_args.args
    assert args[0].__name__ == "generate_croll"
    assert args[2].endswith("source_photo.jpg")
    assert args[3] == "en"
    assert args[4] == "test hint"

    job = get_job(job_id)
    assets = get_assets(job)
    assert len(assets) == 2
    assert all(a["role"] == "broll" for a in assets)
    assert assets[0]["label"] == "office shot" and assets[0]["kind"] == "video"
    assert assets[1]["label"] == "logo closeup" and assets[1]["kind"] == "image"
    assert Path(assets[0]["local_path"]).read_bytes() == b"fake-mp4-bytes"
    assert Path(assets[1]["local_path"]).read_bytes() == b"fake-png-bytes"


def test_croll_without_broll_is_unchanged_from_before(cleanup_jobs):
    """向后兼容：Node 网关今天调用 /croll 时不带 broll 参数——这个场景必须
    跟改动前的行为完全一样（不多注册任何资产，不影响 generate_croll 的
    调用方式）。"""
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        resp = asyncio.run(webhook.create_croll_endpoint(
            photo=_upload("photo.jpg", b"fake-jpeg"),
            hint="",
            lang="zh",
            pipeline="talking-head",
            # 直接调用函数绕开了 FastAPI 的请求解析层——broll/broll_labels/
            # broll_kinds 的 File()/Form() 默认值是路由层用的依赖注入标记，
            # 不会像真实 HTTP 请求那样被框架解析成空列表，这里要显式传
            # 空列表，才是"调用方没传 broll"这个场景真实对应的参数状态。
            broll=[],
            broll_labels=[],
            broll_kinds=[],
            wa_number="api_user",
        ))

    job_id = resp["job_id"]
    cleanup_jobs.append(job_id)
    assert resp["status"] == "RECEIVED"

    assert fake_bg.called
    job = get_job(job_id)
    assert get_assets(job) == []


# ─────────────────────────── POST /transcribe（语音消息转文字） ───────────────────────────
#
# 真实验证过（架构复审后新增，2026-07-29）：拿一段真实视频的音轨转成
# WhatsApp 语音消息实际使用的编码（OGG/Opus），直接调用这个端点，转写结果
# 跟已知内容完全一致——下面这几条是 mock 掉 Transcriber 之后的单元级测试，
# 不需要在仓库里搭一份真实音频 fixture、也不需要 CI 环境装 whisper 模型；
# 传输层/编码兼容性已经用真实调用确认过。

class _FakeTranscribeResult:
    def __init__(self, success, data=None, error=""):
        self.success = success
        self.data = data or {}
        self.error = error


def test_transcribe_endpoint_returns_joined_segment_text(tmp_path):
    fake_result = _FakeTranscribeResult(True, data={
        "segments": [{"text": "帮我把这段去掉"}, {"text": "然后加一段办公室的画面"}],
        "language": "zh",
    })
    with mock.patch("tools.analysis.transcriber.Transcriber.execute", return_value=fake_result):
        result = asyncio.run(webhook.transcribe_endpoint(audio=_upload("voice.ogg", b"fake-ogg-bytes")))

    assert result["text"] == "帮我把这段去掉 然后加一段办公室的画面"
    assert result["language"] == "zh"


def test_transcribe_endpoint_returns_empty_text_on_failure_not_an_exception():
    """转写失败（模型不可用/音频损坏）不能让整个 webhook 处理链路炸掉——
    优雅返回空文本 + 错误信息，调用方（Node）按"没听清"处理。"""
    fake_result = _FakeTranscribeResult(False, error="faster-whisper not available")
    with mock.patch("tools.analysis.transcriber.Transcriber.execute", return_value=fake_result):
        result = asyncio.run(webhook.transcribe_endpoint(audio=_upload("voice.ogg", b"fake-ogg-bytes")))

    assert result["text"] == ""
    assert result["error"]


def test_transcribe_endpoint_cleans_up_its_temp_file():
    """每次调用都会现写一个临时文件给 Transcriber 用——用完必须删掉，不能
    每来一条语音消息就在 /tmp 底下攒一个文件，长期跑下去会把磁盘写满。"""
    captured_path = {}

    def fake_execute(self, inputs):
        captured_path["path"] = inputs["input_path"]
        assert Path(inputs["input_path"]).exists()  # 调用时文件必须存在
        return _FakeTranscribeResult(True, data={"segments": [], "language": "en"})

    with mock.patch("tools.analysis.transcriber.Transcriber.execute", fake_execute):
        asyncio.run(webhook.transcribe_endpoint(audio=_upload("voice.ogg", b"fake-ogg-bytes")))

    assert captured_path.get("path"), "Transcriber.execute 应该被调用过"
    assert not Path(captured_path["path"]).exists(), "临时音频文件用完后应该被删除"


def test_transcribe_endpoint_handles_empty_transcription_result():
    """转写"成功"但完全没说话/听不清（segments 为空）——不应该报错，返回
    空字符串就好，Node 侧会当成"没听清"友好提示。"""
    fake_result = _FakeTranscribeResult(True, data={"segments": [], "language": None})
    with mock.patch("tools.analysis.transcriber.Transcriber.execute", return_value=fake_result):
        result = asyncio.run(webhook.transcribe_endpoint(audio=_upload("voice.ogg", b"fake-ogg-bytes")))

    assert result["text"] == ""
