#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whatsapp_mvp/authored/arm_router.py —— Arm A / Arm B 分层路由(替代会话级 env 开关)。

诉求:显式切换,不靠"改环境变量+重启服务"这种会话级、全进程的粗开关。
按"越具体越优先"分层解析,返回 "arm_a" | "arm_b"(默认 arm_a):

  0. 配置文件 force_arm     —— 全局硬钉/急停开关(Arm B 挂了一键全回 A),压过一切
  1. 本条消息显式标签       —— caption/指令里 "#armb" / "#arma",per-request、免重启
  2. 本条消息自然语言       —— 如 "用AI剪"→b、"套用模板"→a(词表可配、可关;歧义则跳过)
  3. 该用户持久偏好         —— authored_prefs.json[whatsapp_id],跨会话跨重启存活
  4. 配置文件灰度百分比     —— authored_config.json.arm_b_percent(按 job.id 稳定哈希)
  5. 环境变量               —— ARM_B_ENABLED/ARM_B_PERCENT,降为最低优先级的运维兜底
  6. 默认                   —— arm_a

配置/偏好文件目录:环境变量 ARM_ROUTER_DIR 覆盖 → 否则 config.openmontage_root → 否则 cwd。
文件缺失/损坏一律忽略(不 crash),自然回落下一层。读多写少;写偏好用原子替换。

文件格式:
  authored_config.json {
    "force_arm": "arm_a"|"arm_b"|null,   // 急停/硬钉,省缺=不钉
    "arm_b_percent": 0-100|null,          // 灰度百分比,省缺=看 env
    "natural_triggers": true,             // 是否启用自然语言词表(生产可关,防误路由)
    "arm_b_words": [...], "arm_a_words": [...]   // 覆盖默认词表(可选)
  }
  authored_prefs.json { "<whatsapp_id>": "arm_a"|"arm_b", ... }
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

ARM_A, ARM_B = "arm_a", "arm_b"

# 显式标签(无歧义,永远最先认;大小写不敏感)
TAG_B, TAG_A = "#armb", "#arma"

# 自然语言默认词表(保守、具体,避免和正常剪辑指令撞;可被配置文件覆盖/整体关闭)
DEFAULT_B_WORDS = ["用ai剪", "ai剪", "ai帮我剪", "智能剪", "ai编辑", "ai生成剪辑"]
DEFAULT_A_WORDS = ["套用模板", "用模板", "模板剪", "固定模板"]

CONFIG_FILE = "authored_config.json"
PREFS_FILE = "authored_prefs.json"
ARM_CHOICE_FILE = "arm_choice.txt"   # 本 job 显式臂选择,落在 job_dir 下


# ─────────────────────────── 文件 IO(容错)───────────────────────────

def _router_dir() -> Path:
    d = os.getenv("ARM_ROUTER_DIR")
    if d:
        return Path(d)
    try:
        from ..config import get_config
        root = getattr(get_config(), "openmontage_root", "") or ""
        if root:
            return Path(root)
    except Exception:
        pass
    return Path(".")


def _load_json(name: str) -> dict:
    try:
        p = _router_dir() / name
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _config() -> dict:
    return _load_json(CONFIG_FILE)


def _prefs() -> dict:
    return _load_json(PREFS_FILE)


def _from_job_choice(job) -> str | None:
    """本 job 的显式选择(用户在 WhatsApp 点按钮 / 回数字选的臂),写在
    job_dir/arm_choice.txt。优先级仅次于运维急停 force_arm,压过标签/自然语言/
    偏好/灰度/env —— 用户当面点的,就该说了算。缺文件/坏内容一律 None(下一层兜)。"""
    try:
        jd = getattr(job, "job_dir", None)
        if not jd:
            return None
        p = Path(jd) / ARM_CHOICE_FILE
        if p.exists():
            v = p.read_text(encoding="utf-8").strip().lower()
            if v in (ARM_A, ARM_B):
                return v
    except Exception:
        pass
    return None


def set_job_arm(job_dir, arm: str) -> bool:
    """写本 job 的显式臂选择(供 /jobs 收到 arm 字段时调用)。arm ∈ {arm_a,arm_b}
    则写入;其它值(空/非法)=清除已有选择。写失败返回 False(不外抛)。"""
    try:
        d = Path(job_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / ARM_CHOICE_FILE
        if arm in (ARM_A, ARM_B):
            p.write_text(arm, encoding="utf-8")
        elif p.exists():
            p.unlink()
        return True
    except Exception:
        return False


def set_user_pref(whatsapp_id: str, arm: str) -> bool:
    """设/清某用户的持久偏好(arm ∈ {arm_a,arm_b};其它值=清除)。原子写。
    供将来"用户发一句'默认用AI剪'"的指令处理器调用;现在也可手工编辑文件。"""
    if not whatsapp_id:
        return False
    prefs = _prefs()
    if arm in (ARM_A, ARM_B):
        prefs[str(whatsapp_id)] = arm
    else:
        prefs.pop(str(whatsapp_id), None)
    try:
        d = _router_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, str(d / PREFS_FILE))   # 原子替换
        return True
    except Exception:
        return False


# ─────────────────────────── 各层判定 ───────────────────────────

def _job_text(job) -> str:
    parts = [str(getattr(job, "edit_request", "") or ""),
             str(getattr(job, "input_caption", "") or "")]
    return " ".join(parts).lower()


def _from_tag(text: str) -> str | None:
    has_b, has_a = TAG_B in text, TAG_A in text
    if has_b and not has_a:
        return ARM_B
    if has_a and not has_b:
        return ARM_A
    return None   # 都没有 / 都有(歧义)→ 交下一层


def _from_natural(text: str, cfg: dict) -> str | None:
    if cfg.get("natural_triggers", True) is False:
        return None
    b_words = cfg.get("arm_b_words") or DEFAULT_B_WORDS
    a_words = cfg.get("arm_a_words") or DEFAULT_A_WORDS
    hit_b = any(w.lower() in text for w in b_words)
    hit_a = any(w.lower() in text for w in a_words)
    if hit_b and not hit_a:
        return ARM_B
    if hit_a and not hit_b:
        return ARM_A
    return None   # 无命中 / 两边都命中(歧义)→ 交下一层


def _from_user_pref(job) -> str | None:
    wid = getattr(getattr(job, "user", None), "whatsapp_id", "") or ""
    val = _prefs().get(str(wid))
    return val if val in (ARM_A, ARM_B) else None


def _percent_arm(job, percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    if percent >= 100:
        return ARM_B
    if percent <= 0:
        return ARM_A
    h = int(hashlib.sha256(str(getattr(job, "id", "")).encode()).hexdigest()[:8], 16)
    return ARM_B if (h % 100) < percent else ARM_A


def _from_config_percent(job, cfg: dict) -> str | None:
    p = cfg.get("arm_b_percent")
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        return _percent_arm(job, int(p))
    return None


def _from_env(job) -> str | None:
    if os.getenv("ARM_B_ENABLED", "0") != "1":
        return None
    try:
        percent = int(os.getenv("ARM_B_PERCENT", "100"))
    except ValueError:
        percent = 100
    return _percent_arm(job, percent)


# ─────────────────────────── 入口 ───────────────────────────

def resolve_arm(job) -> str:
    cfg = _config()
    text = _job_text(job)

    # 0. 急停/硬钉(压过一切):运维把 Arm B 一键全回 A 用
    force = cfg.get("force_arm")
    if force in (ARM_A, ARM_B):
        return force

    # 0.5 本 job 显式选择(WhatsApp 点选/回数字):仅次于急停,压过下面所有层
    choice = _from_job_choice(job)
    if choice in (ARM_A, ARM_B):
        return choice

    # 1→5 依次,首个非空即采用
    for layer in (_from_tag(text),
                  _from_natural(text, cfg),
                  _from_user_pref(job),
                  _from_config_percent(job, cfg),
                  _from_env(job)):
        if layer in (ARM_A, ARM_B):
            return layer

    return ARM_A   # 6. 默认


def explain(job) -> dict:
    """调试用:返回每层各自的判定,方便看"这条为什么走了 X"。"""
    cfg = _config()
    text = _job_text(job)
    return {
        "resolved": resolve_arm(job),
        "force_arm": cfg.get("force_arm"),
        "job_choice": _from_job_choice(job),
        "tag": _from_tag(text),
        "natural": _from_natural(text, cfg),
        "user_pref": _from_user_pref(job),
        "config_percent": _from_config_percent(job, cfg),
        "env": _from_env(job),
    }