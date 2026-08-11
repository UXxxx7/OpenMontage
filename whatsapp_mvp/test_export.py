# 导出转码命令/预估的单元测试。
#
# build_export_ffmpeg_cmd(resolution="1080p", quality="high") 跟
# run_final_export 原来的裸命令刻意逐位一致（除了新增的 -nostdin）——这是
# 重构安全性的证明，不是巧合：run_final_export 现在就是调用这个函数构造出
# 同一条命令。第一条测试就是这个不变量的回归防护——它变了，就说明 WhatsApp
# 最终导出的行为被意外改动了。
#
# 都是纯函数测试，不跑真实 ffmpeg（不需要网络/子进程/真实素材）。

from __future__ import annotations

import pytest

from whatsapp_mvp.pipeline_runner import (
    EXPORT_QUALITIES,
    EXPORT_RESOLUTIONS,
    build_export_ffmpeg_cmd,
    estimate_export_bytes,
    resolve_export_dimensions,
)


def test_1080p_high_matches_run_final_export_original_command():
    """WhatsApp 最终导出用的正是 resolution=1080p quality=high 这一档——
    这条命令必须跟改动前 run_final_export 里硬编码的裸命令逐位一致（除了
    新增的 -nostdin），否则就是一次静默的行为改动。"""
    cmd = build_export_ffmpeg_cmd(
        "preview.mp4", "final.mp4", resolution="1080p", quality="high",
        src_w=1080, src_h=1920,
    )
    assert cmd == [
        "ffmpeg", "-nostdin", "-y", "-i", "preview.mp4",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "final.mp4",
    ]


def test_1080p_high_has_no_scale_filter_when_source_already_matches():
    """源已经是目标分辨率——不应该多此一举加 -vf scale。"""
    cmd = build_export_ffmpeg_cmd(
        "preview.mp4", "final.mp4", resolution="1080p", quality="high",
        src_w=1080, src_h=1920,
    )
    assert not any("scale=" in part for part in cmd)


def test_720p_downscales_with_lanczos():
    cmd = build_export_ffmpeg_cmd(
        "preview.mp4", "final.mp4", resolution="720p", quality="balanced",
        src_w=1080, src_h=1920,
    )
    assert "-vf" in cmd
    idx = cmd.index("-vf")
    assert cmd[idx + 1] == "scale=720:1280:flags=lanczos"


def test_never_upscales():
    """源已经比目标分辨率小——绝不放大，直接原样输出。"""
    assert resolve_export_dimensions(720, 1280, "1080p") == (720, 1280)


def test_odd_source_dimensions_snap_to_even():
    """h264 + yuv420p 色度采样要求宽高都是偶数——奇数源必须被修正，不能
    原样传给 ffmpeg 导致编码失败。"""
    w, h = resolve_export_dimensions(607, 1080, "720p")
    assert w % 2 == 0
    assert h % 2 == 0


def test_landscape_source_scales_by_width():
    """横屏源（宽 > 高）——长边是宽，720p 应该把宽缩到 1280，不是高。"""
    w, h = resolve_export_dimensions(1920, 1080, "720p")
    assert w == 1280
    assert h < 1080


def test_no_fps_pix_fmt_or_sample_rate_flags_in_any_combo():
    """三个刻意不加的 flag——每种分辨率x画质组合都不能出现，防止未来"顺手"
    加回去（-pix_fmt 会导致电平跟预览不一致，-r 会插帧，-ar 会重采样丢真实
    素材）。"""
    for resolution in EXPORT_RESOLUTIONS:
        for quality in EXPORT_QUALITIES:
            cmd = build_export_ffmpeg_cmd(
                "preview.mp4", "final.mp4", resolution=resolution, quality=quality,
                src_w=1080, src_h=1920,
            )
            assert "-pix_fmt" not in cmd
            assert "-r" not in cmd
            assert "-ar" not in cmd


def test_unknown_resolution_raises():
    with pytest.raises(ValueError):
        build_export_ffmpeg_cmd("a.mp4", "b.mp4", resolution="4k", quality="high",
                                src_w=1080, src_h=1920)


def test_unknown_quality_raises():
    with pytest.raises(ValueError):
        build_export_ffmpeg_cmd("a.mp4", "b.mp4", resolution="1080p", quality="lossless",
                                src_w=1080, src_h=1920)


def test_export_output_name_never_contains_a_path_separator():
    """filename 直接来自 f"export_{resolution}_{quality}.mp4"——两个变量都是
    经过枚举校验的常量键，永远不可能包含路径分隔符，但这里显式断言一次，
    把这个不变量钉死在测试里。"""
    for resolution in EXPORT_RESOLUTIONS:
        for quality in EXPORT_QUALITIES:
            name = f"export_{resolution}_{quality}.mp4"
            assert "/" not in name
            assert "\\" not in name


# 实测数据（job_d7d5c007bbc0，1080x1920/30fps/44.83s 真实 Remotion 输出，
# -f null - 编码测量）：
#   1080p high≈15.0MB balanced≈8.5MB small≈4.7MB
#   720p  high≈7.9MB  balanced≈4.2MB  small≈2.5MB
_MEASURED_BYTES = {
    ("1080p", "high"): 15_000_000, ("1080p", "balanced"): 8_500_000, ("1080p", "small"): 4_700_000,
    ("720p", "high"): 7_900_000, ("720p", "balanced"): 4_200_000, ("720p", "small"): 2_500_000,
}


@pytest.mark.parametrize("resolution,quality", list(_MEASURED_BYTES.keys()))
def test_estimate_within_25_percent_of_measured(resolution, quality):
    estimated = estimate_export_bytes(
        resolution=resolution, quality=quality, src_w=1080, src_h=1920,
        fps=30, duration_s=44.83,
    )
    measured = _MEASURED_BYTES[(resolution, quality)]
    assert measured * 0.75 <= estimated <= measured * 1.25, (
        f"{resolution}/{quality}: estimated={estimated} measured={measured}"
    )
