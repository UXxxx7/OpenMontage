# qa_stills.review_stills 的单测——不依赖真实视觉 LLM，monkeypatch
# qa_stills 自己命名空间里的 call_llm_vision（qa_stills.py 是
# `from .llm_client import call_llm_vision`，patch 后者对已绑定的名字无效）。
#
# Run: uv run python -m whatsapp_mvp.test_qa_stills_vision

from __future__ import annotations

import json

import whatsapp_mvp.qa_stills as qa_stills

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


STILLS = [
    {"frame": 100, "path": "/fake/qa_f100.png"},
    {"frame": 200, "path": "/fake/qa_f200.png"},
]


def test_mixed_findings():
    def fake(system_prompt, user_message, images, *, temperature=0.1):
        return json.dumps({"findings": [
            {"frame": 100, "issue": "face cropped out", "severity": "major"},
            {"frame": 200, "issue": "slightly off-center", "severity": "minor"},
        ]})

    qa_stills.call_llm_vision = fake
    findings = qa_stills.review_stills(STILLS)
    check("mixed findings: 2 条都通过", len(findings) == 2, findings)
    check("mixed findings: check 字段固定为 vision_review",
          all(f["check"] == "vision_review" for f in findings), findings)
    check("mixed findings: severity 原样保留",
          {f["frame"]: f["severity"] for f in findings} == {100: "major", 200: "minor"}, findings)


def test_empty_findings():
    def fake(system_prompt, user_message, images, *, temperature=0.1):
        return json.dumps({"findings": []})

    qa_stills.call_llm_vision = fake
    findings = qa_stills.review_stills(STILLS)
    check("空 findings -> 空列表", findings == [], findings)


def test_unparseable_json():
    def fake(system_prompt, user_message, images, *, temperature=0.1):
        return "this is not json at all"

    qa_stills.call_llm_vision = fake
    try:
        findings = qa_stills.review_stills(STILLS)
        raised = False
    except Exception:
        raised = True
        findings = None
    check("解析失败不抛异常", not raised)
    check("解析失败 -> 空列表", findings == [], findings)


def test_none_response():
    def fake(system_prompt, user_message, images, *, temperature=0.1):
        return None

    qa_stills.call_llm_vision = fake
    findings = qa_stills.review_stills(STILLS)
    check("call_llm_vision 返回 None -> 空列表", findings == [], findings)


def test_empty_stills_short_circuits():
    calls = {"n": 0}

    def fake(system_prompt, user_message, images, *, temperature=0.1):
        calls["n"] += 1
        return json.dumps({"findings": []})

    qa_stills.call_llm_vision = fake
    findings = qa_stills.review_stills([])
    check("空 stills -> 空列表，且不调用 call_llm_vision", findings == [] and calls["n"] == 0, calls)


def main():
    test_mixed_findings()
    test_empty_findings()
    test_unparseable_json()
    test_none_response()
    test_empty_stills_short_circuits()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All qa_stills vision-review tests passed.")


if __name__ == "__main__":
    main()
