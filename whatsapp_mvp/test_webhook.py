# POST /croll 的 b-roll 参数回归测试（架构复审后新增，2026-07-29）。
#
# 之前 /croll 只接收 photo/hint/lang/pipeline，没有 broll 相关参数——图生
# 视频（HeyGen 数字人）生成出的 job 上永远不会有 role=="broll" 的资产，
# insert_broll 因此永远不会被 L2 规划器 emit，不是管线跑不通，是这个入口
# 没给它素材可用。补齐跟 POST /jobs 完全同一套参数形状/登记逻辑后，这里
# 验证：(1) 传了 broll 时正确落盘+登记进 job.assets，(2) 不传 broll 时跟
# 改动前完全一样（向后兼容，覆盖 Node 网关现有的调用方式）。
#
# 不真的触发 HeyGen（要花钱、要等几分钟）——用 mock 顶掉 _run_in_background，
# 只验证端点自己同步做的落盘/登记那部分。
#
# Run: uv run python -m pytest whatsapp_mvp/test_webhook.py

from __future__ import annotations

import io
import unittest.mock as mock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import whatsapp_mvp.webhook as webhook
from whatsapp_mvp.job_manager import get_assets, get_job, get_session
from whatsapp_mvp.database import Job


@pytest.fixture
def client():
    return TestClient(webhook.app)


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


def test_croll_registers_broll_assets(client, cleanup_jobs):
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        resp = client.post(
            "/croll",
            files=[
                ("photo", ("photo.jpg", io.BytesIO(b"fake-jpeg"), "image/jpeg")),
                ("broll", ("clip1.mp4", io.BytesIO(b"fake-mp4-bytes"), "video/mp4")),
                ("broll", ("clip2.png", io.BytesIO(b"fake-png-bytes"), "image/png")),
            ],
            data={
                "hint": "test hint",
                "lang": "en",
                "broll_labels": ["office shot", "logo closeup"],
                "broll_kinds": ["", ""],
            },
        )

    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    cleanup_jobs.append(job_id)

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


def test_croll_without_broll_is_unchanged_from_before(client, cleanup_jobs):
    """向后兼容：Node 网关今天调用 /croll 时不带 broll 参数——这个场景必须
    跟改动前的行为完全一样（不多注册任何资产，不影响 generate_croll 的
    调用方式）。"""
    with mock.patch.object(webhook, "_run_in_background") as fake_bg:
        resp = client.post(
            "/croll",
            files={"photo": ("photo.jpg", io.BytesIO(b"fake-jpeg"), "image/jpeg")},
            data={"hint": "", "lang": "zh", "pipeline": "talking-head"},
        )

    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    cleanup_jobs.append(job_id)

    assert fake_bg.called
    job = get_job(job_id)
    assert get_assets(job) == []
