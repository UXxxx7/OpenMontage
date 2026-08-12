# 浏览器编辑器"上传自己的背景音乐"——单元测试。
#
# 覆盖两层：webhook.py 的上传/删除路由本身（格式校验、大小上限、
# 一次只保留一份的替换逻辑），以及 pipeline_runner.pin_music_src_prop
# 这道安全闸门（musicSrc 永远从磁盘真实文件重新推导，绝不信任客户端提交
# 的值——跟 pin_server_owned_props 对 videoSrc 的处理是同一个威胁模型）。
#
# 不用 fastapi.testclient/httpx，理由跟 test_webhook.py 顶部注释一致：
# 直接当普通 Python 函数调用端点，自己构造 UploadFile。
#
# Run: uv run python -m pytest whatsapp_mvp/test_editor_music.py

from __future__ import annotations

import asyncio
import io

import pytest
from starlette.datastructures import UploadFile

import whatsapp_mvp.webhook as webhook
from whatsapp_mvp.editor_token import make_token
from whatsapp_mvp.job_manager import create_job, get_job, get_or_create_user, get_session
from whatsapp_mvp.database import Job
from whatsapp_mvp.pipeline_runner import pin_music_src_prop


def _upload(filename: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=filename, headers={"content-type": content_type})


@pytest.fixture
def music_job():
    user = get_or_create_user("editor_music_test_user")
    job = create_job(user_id=user.id)
    job.job_dir.mkdir(parents=True, exist_ok=True)
    token = make_token(job.id)
    yield job, token
    j = get_job(job.id)
    if j is not None:
        import shutil
        shutil.rmtree(j.job_dir, ignore_errors=True)
    session = get_session()
    try:
        session.query(Job).filter(Job.id == job.id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


# ─────────────────────────── POST /editor/{id}/music ───────────────────────────

def test_upload_accepts_mp3_and_writes_to_disk(music_job):
    job, token = music_job
    result = asyncio.run(webhook.editor_post_music(
        job.id, token, file=_upload("track.mp3", b"fake-mp3-bytes", "audio/mpeg")))
    assert result["filename"] == "_editor_music.mp3"
    assert (job.job_dir / "_editor_music.mp3").read_bytes() == b"fake-mp3-bytes"
    assert result["bytes"] == len(b"fake-mp3-bytes")
    assert "music_url" in result


def test_upload_falls_back_to_filename_extension_when_content_type_is_generic(music_job):
    """浏览器有时候给 .m4a/.aac 这类容器格式报一个通用/错误的
    content-type（如 application/octet-stream）——只要文件名自带的扩展名
    落在白名单里，也该放行，不能卡在一个不可靠的浏览器猜测上。"""
    job, token = music_job
    result = asyncio.run(webhook.editor_post_music(
        job.id, token, file=_upload("song.m4a", b"fake-m4a-bytes", "application/octet-stream")))
    assert result["filename"] == "_editor_music.m4a"


def test_upload_rejects_unsupported_format(music_job):
    job, token = music_job
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_music(
            job.id, token, file=_upload("virus.exe", b"MZ...", "application/x-msdownload")))
    assert exc_info.value.status_code == 400


def test_upload_rejects_oversized_file(music_job):
    job, token = music_job
    huge = b"x" * (webhook._EDITOR_MUSIC_MAX_BYTES + 1)
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_music(
            job.id, token, file=_upload("big.mp3", huge, "audio/mpeg")))
    assert exc_info.value.status_code == 413
    # 拒绝的上传不该在磁盘上留下任何痕迹。
    assert not any(job.job_dir.glob("_editor_music.*"))


def test_upload_rejects_empty_file(music_job):
    job, token = music_job
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_music(
            job.id, token, file=_upload("empty.mp3", b"", "audio/mpeg")))
    assert exc_info.value.status_code == 400


def test_second_upload_replaces_the_first_even_across_formats(music_job):
    """一个 job 同时最多一份——重新上传（哪怕换了格式）必须先删掉旧文件，
    不能留下 _editor_music.mp3 和 _editor_music.wav 两份同时存在，
    否则 pin_music_src_prop 的 glob 会撞见不止一个文件。"""
    job, token = music_job
    asyncio.run(webhook.editor_post_music(
        job.id, token, file=_upload("first.mp3", b"first-bytes", "audio/mpeg")))
    asyncio.run(webhook.editor_post_music(
        job.id, token, file=_upload("second.wav", b"second-bytes-longer", "audio/wav")))

    remaining = list(job.job_dir.glob("_editor_music.*"))
    assert len(remaining) == 1
    assert remaining[0].name == "_editor_music.wav"
    assert remaining[0].read_bytes() == b"second-bytes-longer"


def test_upload_requires_valid_editor_token(music_job):
    job, _token = music_job
    with pytest.raises(webhook.HTTPException) as exc_info:
        asyncio.run(webhook.editor_post_music(
            job.id, "not-a-real-token", file=_upload("track.mp3", b"bytes", "audio/mpeg")))
    assert exc_info.value.status_code == 403


# ─────────────────────────── DELETE /editor/{id}/music ───────────────────────────

def test_delete_removes_an_existing_upload(music_job):
    job, token = music_job
    asyncio.run(webhook.editor_post_music(
        job.id, token, file=_upload("track.mp3", b"bytes", "audio/mpeg")))
    result = webhook.editor_delete_music(job.id, token)
    assert result["deleted"] is True
    assert not any(job.job_dir.glob("_editor_music.*"))


def test_delete_is_a_no_op_when_nothing_was_uploaded(music_job):
    job, token = music_job
    result = webhook.editor_delete_music(job.id, token)
    assert result["deleted"] is False


# ─────────────────────────── pin_music_src_prop ───────────────────────────

def test_pin_music_src_sets_url_when_a_file_exists_on_disk(music_job):
    job, _token = music_job
    (job.job_dir / "_editor_music.mp3").write_bytes(b"bytes")
    props = pin_music_src_prop({"musicSrc": "whatever-the-client-sent"}, job.job_dir, job.id)
    assert props["musicSrc"].endswith(f"/files/{job.id}/_editor_music.mp3")


def test_pin_music_src_ignores_client_value_and_uses_disk_truth(music_job):
    """安全闸门的核心断言：客户端提交的 musicSrc 永远不会原样通过，不管
    它是良性字符串还是一次注入尝试（file:///, http://internal-host/...）——
    这里特意提交一个看起来像攻击载荷的值，确认它被整个丢弃。"""
    job, _token = music_job
    (job.job_dir / "_editor_music.wav").write_bytes(b"bytes")
    props = pin_music_src_prop({"musicSrc": "file:///etc/passwd"}, job.job_dir, job.id)
    assert props["musicSrc"] != "file:///etc/passwd"
    assert props["musicSrc"].endswith(f"/files/{job.id}/_editor_music.wav")


def test_pin_music_src_strips_field_when_no_file_exists(music_job):
    """客户端提交了一个 musicSrc，但磁盘上根本没有上传过任何文件——必须
    整个字段被丢弃，不能放行一个指向不存在文件的 URL 进渲染。"""
    job, _token = music_job
    props = pin_music_src_prop({"musicSrc": "file:///etc/passwd"}, job.job_dir, job.id)
    assert "musicSrc" not in props


def test_pin_music_src_leaves_other_fields_untouched(music_job):
    job, _token = music_job
    props = pin_music_src_prop({"videoSrc": "keep-me", "musicVolume": 0.2}, job.job_dir, job.id)
    assert props["videoSrc"] == "keep-me"
    assert props["musicVolume"] == 0.2
    assert "musicSrc" not in props
