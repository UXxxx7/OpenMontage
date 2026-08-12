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
from whatsapp_mvp.job_manager import create_job, get_assets, get_job, get_or_create_user, get_session
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


# GET /files/{job_id}/{filename} 路径穿越回归测试。
#
# 确认过真实可复现：file_path = job.job_dir / filename 没有任何包含性检查，
# {filename} 编译成 [^/]+（不允许正斜杠），但不挡反斜杠——Windows 上
# pathlib 把反斜杠当成路径分隔符处理，filename="..\\..\\..\\.env" 能干净地
# 解析到 job_dir 之外，实测直接读出了这个仓库真正的 .env（API key）和
# sqlite 数据库（每个用户的 WhatsApp 号码）。这条路由本身无鉴权（job_id
# 就是唯一的"能力凭证"），经 ngrok 隧道对外可达。

@pytest.fixture
def real_job(cleanup_jobs):
    """跟其它端点测试一样直接建一条真实 job（不 mock get_job），因为
    serve_file 的漏洞就在 job.job_dir 之外那一层，必须是真实磁盘路径。"""
    user = get_or_create_user("traversal_test_user")
    job = create_job(user_id=user.id)
    cleanup_jobs.append(job.id)
    return job


def test_serve_file_rejects_windows_backslash_traversal(real_job):
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "..\\..\\..\\.env")
    assert exc_info.value.status_code == 404


def test_serve_file_rejects_forward_slash_traversal(real_job):
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "../../../.env")
    assert exc_info.value.status_code == 404


def test_serve_file_rejects_absolute_windows_path(real_job):
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "C:\\Windows\\win.ini")
    assert exc_info.value.status_code == 404


def test_serve_file_rejects_traversal_to_the_database(real_job):
    """.env 之外，第二个真实敏感目标：sqlite 数据库本身。"""
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "..\\..\\..\\openmontage_whatsapp.db")
    assert exc_info.value.status_code == 404


def test_serve_file_still_serves_a_real_file_inside_job_dir(real_job):
    """安全修复不能连正常路径一起挡掉——这是回归防护，不是新增行为。"""
    (real_job.job_dir / "preview.mp4").write_bytes(b"fake-mp4-bytes")
    response = webhook.serve_file(real_job.id, "preview.mp4")
    assert response.media_type == "video/mp4"


def test_serve_file_rejects_nonexistent_file(real_job):
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "does_not_exist.mp4")
    assert exc_info.value.status_code == 404


def test_serve_file_rejects_a_directory(real_job):
    """filename 指向一个目录（而不是文件）也必须 404，不能让 FileResponse
    拿一个目录路径去尝试打开报出别的错误类型。"""
    (real_job.job_dir / "assets").mkdir(parents=True, exist_ok=True)
    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.serve_file(real_job.id, "assets")
    assert exc_info.value.status_code == 404


# GET/POST /editor/{job_id}/export* —— 浏览器编辑器"导出"三个路由。
# 用真实签发的 token（editor_token.make_token）而不是 mock verify_token——
# 跟 make_token/verify_token 的真实实现走一遍，比 mock 掉鉴权更接近生产
# 行为。用 mock 顶掉 _run_in_background，实际的 ffmpeg 转码不在这些测试
# 范围内（那部分由 test_export.py 的纯函数测试覆盖）。

@pytest.fixture
def export_job(cleanup_jobs):
    from whatsapp_mvp.editor_token import make_token

    user = get_or_create_user("export_test_user")
    job = create_job(user_id=user.id)
    job.job_dir.mkdir(parents=True, exist_ok=True)
    (job.job_dir / "preview.mp4").write_bytes(b"fake-preview-mp4-bytes" * 100)
    cleanup_jobs.append(job.id)
    token = make_token(job.id)
    return job, token


def test_editor_export_options_404_without_preview(cleanup_jobs):
    from whatsapp_mvp.editor_token import make_token

    user = get_or_create_user("export_test_user_no_preview")
    job = create_job(user_id=user.id)
    job.job_dir.mkdir(parents=True, exist_ok=True)
    cleanup_jobs.append(job.id)
    token = make_token(job.id)

    with pytest.raises(webhook.HTTPException) as exc_info:
        webhook.editor_export_options(job.id, token)
    assert exc_info.value.status_code == 404


def test_editor_export_options_lists_six_combos(export_job):
    job, token = export_job
    with mock.patch("whatsapp_mvp.pipeline_runner._probe_dimensions", return_value=(1080, 1920)), \
         mock.patch("whatsapp_mvp.pipeline_runner._probe_duration", return_value=45.0):
        result = webhook.editor_export_options(job.id, token)

    assert result["source"]["width"] == 1080
    assert result["source"]["height"] == 1920
    assert result["source"]["fps"] == 30
    assert len(result["combos"]) == 6
    assert all(c["cached"] is False for c in result["combos"])
    resolutions = {(c["resolution"], c["quality"]) for c in result["combos"]}
    assert ("1080p", "high") in resolutions
    assert ("720p", "small") in resolutions


def test_editor_export_options_reports_cache_hit(export_job):
    """已经转出来的一档、且比 preview.mp4 新——上报 cached:true 和精确的
    cached_bytes，不该再让前端拿估算值。"""
    job, token = export_job
    export_path = job.job_dir / "export_720p_balanced.mp4"
    export_path.write_bytes(b"already-exported" * 1000)

    with mock.patch("whatsapp_mvp.pipeline_runner._probe_dimensions", return_value=(1080, 1920)), \
         mock.patch("whatsapp_mvp.pipeline_runner._probe_duration", return_value=45.0):
        result = webhook.editor_export_options(job.id, token)

    combo = next(c for c in result["combos"] if c["resolution"] == "720p" and c["quality"] == "balanced")
    assert combo["cached"] is True
    assert combo["cached_bytes"] == export_path.stat().st_size
    assert combo["estimated_bytes"] == export_path.stat().st_size


def test_editor_export_post_rejects_invalid_enum(export_job):
    job, token = export_job
    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "4k", "quality": "lossless"})
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_export(job.id, request, token))
    assert exc_info.value.status_code == 400


def test_editor_export_post_404_without_preview(cleanup_jobs):
    from whatsapp_mvp.editor_token import make_token

    user = get_or_create_user("export_test_user_post_no_preview")
    job = create_job(user_id=user.id)
    job.job_dir.mkdir(parents=True, exist_ok=True)
    cleanup_jobs.append(job.id)
    token = make_token(job.id)

    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_export(job.id, request, token))
    assert exc_info.value.status_code == 404


def test_editor_export_post_409_while_save_rendering(export_job):
    """跟 /save 唯一的交叉点：保存正在渲染时不能开始导出——preview.mp4
    这时候正在被 render_props_directly 改写，转码一份正在写入的文件不
    安全，也没有意义（马上就会过时）。"""
    job, token = export_job
    webhook._write_editor_marker(job.job_dir, {
        "state": "rendering", "pending_props": None, "started_at": None,
        "error": None, "save_timestamps": [], "pending_overrides": None,
    })

    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_export(job.id, request, token))
    assert exc_info.value.status_code == 409


def test_editor_export_post_does_not_leak_wa_number(export_job):
    """导出响应体绝不能带 wa_number——这不是 WhatsApp 投递流程，跟
    editor_post_props 的 /revise_style 场景完全不同。"""
    job, token = export_job
    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        result = asyncio.run(webhook.editor_post_export(job.id, request, token))

    assert fake_bg.called
    assert "wa_number" not in result
    assert result["state"] == "queued"


def test_editor_export_post_cache_hit_skips_encoding(export_job):
    """这一档已经是基于当前 preview.mp4 转码出的最新结果——直接回
    state:"done"，不排队后台任务，也不消耗配额。"""
    job, token = export_job
    export_path = job.job_dir / "export_720p_balanced.mp4"
    export_path.write_bytes(b"already-exported" * 1000)

    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        result = asyncio.run(webhook.editor_post_export(job.id, request, token))

    assert not fake_bg.called
    assert result["state"] == "done"
    assert result["filename"] == "export_720p_balanced.mp4"
    assert result["bytes"] == export_path.stat().st_size


def test_editor_export_post_coalesces_when_already_encoding(export_job):
    job, token = export_job
    from whatsapp_mvp.editor_markers import write_export_marker

    write_export_marker(job.job_dir, {
        "state": "encoding", "pending": None, "current": {"resolution": "1080p", "quality": "high"},
        "progress": 40, "started_at": None, "error": None, "export_timestamps": [],
    })

    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        result = asyncio.run(webhook.editor_post_export(job.id, request, token))

    assert not fake_bg.called
    assert result["state"] == "coalesced"

    from whatsapp_mvp.editor_markers import read_export_marker
    marker = read_export_marker(job.job_dir)
    assert marker["pending"] == {"resolution": "720p", "quality": "balanced"}


def test_editor_export_post_429_past_hourly_cap(export_job):
    job, token = export_job
    from whatsapp_mvp.editor_markers import write_export_marker

    write_export_marker(job.job_dir, {
        "state": "idle", "pending": None, "current": None, "progress": 0,
        "started_at": None, "error": None,
        "export_timestamps": [webhook.time.time()] * webhook._EDITOR_EXPORTS_PER_HOUR,
    })

    request = mock.MagicMock()
    request.json = mock.AsyncMock(return_value={"resolution": "720p", "quality": "balanced"})
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_export(job.id, request, token))
    assert exc_info.value.status_code == 429


def test_editor_export_status_reports_marker_state(export_job):
    job, token = export_job
    from whatsapp_mvp.editor_markers import write_export_marker

    write_export_marker(job.job_dir, {
        "state": "encoding", "pending": None, "current": {"resolution": "720p", "quality": "balanced"},
        "progress": 55, "started_at": None, "error": None, "export_timestamps": [],
    })

    result = webhook.editor_export_status(job.id, token)
    assert result["state"] == "encoding"
    assert result["progress"] == 55
    assert result["combo"] == {"resolution": "720p", "quality": "balanced"}
    assert "wa_number" not in result
