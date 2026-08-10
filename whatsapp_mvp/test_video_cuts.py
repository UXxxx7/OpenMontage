# video_cuts.py 是 remotion-composer/src/cuts.ts 的手写 Python 移植——服务端有
# 两处需要用 OUTPUT-frame 空间推理（qa_stills 选取抽帧、pipeline_runner clamp
# 客户端提交的 videoCuts）。这份测试不重新验证 cuts.ts 自己的正确性（那边有
# 自己的测试），只验证两边行为一致：contracts/video_cuts_fixture.json 是直接
# 跑 cuts.ts 生成的真实输出，这里逐条比对 video_cuts.py 是否复现同样的值。
#
# Run: uv run python -m whatsapp_mvp.test_video_cuts

from __future__ import annotations

import json
from pathlib import Path

from whatsapp_mvp.video_cuts import (
    compute_output_duration, normalize_cuts, output_to_source, source_to_output,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "contracts" / "video_cuts_fixture.json"

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_normalize_cuts_matches_ts_reference():
    fixture = _load_fixture()
    for i, case in enumerate(fixture["normalizeCases"]):
        got = normalize_cuts(case["input"]["cuts"], case["input"]["srcLen"])
        check(f"normalizeCuts parity case {i}", got == case["output"], f"got {got}, want {case['output']}")


def test_source_to_output_matches_ts_reference():
    fixture = _load_fixture()
    cuts = fixture["testCuts"]
    for case in fixture["mappingCases"]:
        got = source_to_output(case["srcFrame"], cuts)
        check(
            f"sourceToOutput({case['srcFrame']}) parity",
            got == case["outFrame"],
            f"got {got}, want {case['outFrame']}",
        )


def test_output_to_source_matches_ts_reference():
    fixture = _load_fixture()
    cuts = fixture["testCuts"]
    for case in fixture["inverseCases"]:
        got = output_to_source(case["outFrame"], cuts)
        check(
            f"outputToSource({case['outFrame']}) parity",
            got == case["srcFrame"],
            f"got {got}, want {case['srcFrame']}",
        )


def test_source_to_output_and_output_to_source_are_exact_inverses():
    fixture = _load_fixture()
    cuts = fixture["testCuts"]
    for o in range(0, 850, 7):
        src = output_to_source(o, cuts)
        back = source_to_output(src, cuts)
        check(f"sourceToOutput(outputToSource({o})) == {o}", back == o, f"got {back}")


def test_compute_output_duration_matches_ts_reference():
    fixture = _load_fixture()
    for i, case in enumerate(fixture["durationCases"]):
        got = compute_output_duration(case["input"]["props"])
        check(f"computeOutputDuration parity case {i}", got == case["output"], f"got {got}, want {case['output']}")


def test_normalize_cuts_handles_empty_and_none():
    check("normalize_cuts(None, 100) == []", normalize_cuts(None, 100) == [])
    check("normalize_cuts([], 100) == []", normalize_cuts([], 100) == [])


def main():
    test_normalize_cuts_matches_ts_reference()
    test_source_to_output_matches_ts_reference()
    test_output_to_source_matches_ts_reference()
    test_source_to_output_and_output_to_source_are_exact_inverses()
    test_compute_output_duration_matches_ts_reference()
    test_normalize_cuts_handles_empty_and_none()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All video_cuts tests passed.")


if __name__ == "__main__":
    main()
