# worker.py 的 AI 生成 b-roll 额度预警回归测试（架构复审后新增，2026-07-29）。
#
# 真实事故：job_9c671249eb76，规划要求 AI 生成 b-roll，但账号在 Gemini 免费
# 档、对应模型配额是 0——用户直到确认、等生成失败了才知道。这里验证
# _send_confirmation 会在发确认消息*之前*检查一次可用性，命中不可用就把
# 警示写进 job.planned_edit（Node 网关读的就是这个字段自己拼确认消息，见
# server/worker.js formatPlanMessage）。
#
# Run: uv run python -m pytest whatsapp_mvp/test_worker.py

from __future__ import annotations

import json
import shutil
import unittest.mock as mock

import pytest

import whatsapp_mvp.worker as worker
from whatsapp_mvp.job_manager import create_job, get_job, get_or_create_user, get_session, update_job_fields
from whatsapp_mvp.database import Job


# ─────────────────────────── _plan_needs_broll_generation ───────────────────────────

def test_detects_gen_prompt_broll_item():
    ops = [{"type": "insert_broll", "items": [{"gen_prompt": "a cat on a desk", "start_seconds": 1, "end_seconds": 5}]}]
    assert worker._plan_needs_broll_generation(ops) is True


def test_does_not_flag_uploaded_broll_asset_ref():
    """asset_ref（用户上传的素材）不需要 AI 生成，不该被这条检查拦下。"""
    ops = [{"type": "insert_broll", "items": [{"asset_ref": "1", "start_seconds": 1, "end_seconds": 5}]}]
    assert worker._plan_needs_broll_generation(ops) is False


def test_does_not_flag_when_both_asset_ref_and_gen_prompt_present():
    """同一个 item 理论上不该两者都给（L2 规划器的约束），但防御性地——只要
    有 asset_ref 就走上传素材，不该被当成需要生成。"""
    ops = [{"type": "insert_broll", "items": [{"asset_ref": "1", "gen_prompt": "x", "start_seconds": 1, "end_seconds": 5}]}]
    assert worker._plan_needs_broll_generation(ops) is False


def test_does_not_flag_non_broll_operations():
    ops = [{"type": "remove_filler"}, {"type": "apply_style"}]
    assert worker._plan_needs_broll_generation(ops) is False


def test_does_not_flag_empty_or_none_operations():
    assert worker._plan_needs_broll_generation([]) is False
    assert worker._plan_needs_broll_generation(None) is False


def test_detects_gen_prompt_among_multiple_broll_items():
    """一个 insert_broll 操作里可以混着上传素材和 AI 生成两种——只要有一条
    是生成的就该触发检查。"""
    ops = [{"type": "insert_broll", "items": [
        {"asset_ref": "1", "start_seconds": 1, "end_seconds": 5},
        {"gen_prompt": "an office desk", "start_seconds": 10, "end_seconds": 14},
    ]}]
    assert worker._plan_needs_broll_generation(ops) is True


# ─────────────────────────── _send_confirmation ───────────────────────────

@pytest.fixture
def real_job():
    user = get_or_create_user("test_user_send_confirmation")
    job = create_job(user_id=user.id, pipeline="talking-head", input_caption="test")
    job.job_dir.mkdir(parents=True, exist_ok=True)
    yield job.id
    j = get_job(job.id)
    if j is not None:
        shutil.rmtree(j.job_dir, ignore_errors=True)
    session = get_session()
    try:
        session.query(Job).filter(Job.id == job.id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _plan_with_gen_broll():
    return {
        "summary": "去口误 + AI 生成办公室场景 b-roll",
        "edit_operations": [
            {"type": "remove_filler", "description": "去口误"},
            {"type": "insert_broll", "description": "AI 生成办公室场景",
             "items": [{"gen_prompt": "an office desk with a laptop",
                        "start_seconds": 15.5, "end_seconds": 21.5}]},
        ],
    }


def test_send_confirmation_persists_warning_when_broll_generation_unavailable(real_job):
    update_job_fields(real_job, planned_edit=json.dumps(_plan_with_gen_broll(), ensure_ascii=False))
    job = get_job(real_job)

    with mock.patch("whatsapp_mvp.gemini_broll.check_broll_generation_availability",
                     return_value=(False, "AI 生成 b-roll 当前不可用（免费档配额为 0）")), \
         mock.patch.object(worker, "_safe_send"):
        worker._send_confirmation(job, mock.MagicMock())

    updated = get_job(real_job)
    persisted_plan = json.loads(updated.planned_edit)
    assert persisted_plan.get("broll_generation_warning") == "AI 生成 b-roll 当前不可用（免费档配额为 0）"


def test_send_confirmation_does_not_add_warning_when_broll_generation_available(real_job):
    update_job_fields(real_job, planned_edit=json.dumps(_plan_with_gen_broll(), ensure_ascii=False))
    job = get_job(real_job)

    with mock.patch("whatsapp_mvp.gemini_broll.check_broll_generation_availability",
                     return_value=(True, "")), \
         mock.patch.object(worker, "_safe_send"):
        worker._send_confirmation(job, mock.MagicMock())

    updated = get_job(real_job)
    persisted_plan = json.loads(updated.planned_edit)
    assert "broll_generation_warning" not in persisted_plan


def test_send_confirmation_skips_availability_check_when_no_broll_generation_planned(real_job):
    """普通任务（没有 AI 生成 b-roll 的需求）不该触发这条检查——不产生
    无谓的调用，也不该在 plan 里加任何跟这个功能无关的字段。"""
    plan = {"summary": "去口误", "edit_operations": [{"type": "remove_filler", "description": "去口误"}]}
    update_job_fields(real_job, planned_edit=json.dumps(plan, ensure_ascii=False))
    job = get_job(real_job)

    with mock.patch("whatsapp_mvp.gemini_broll.check_broll_generation_availability") as fake_check, \
         mock.patch.object(worker, "_safe_send"):
        worker._send_confirmation(job, mock.MagicMock())

    assert not fake_check.called
    updated = get_job(real_job)
    persisted_plan = json.loads(updated.planned_edit)
    assert "broll_generation_warning" not in persisted_plan
