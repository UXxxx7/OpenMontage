# WhatsApp MVP - 浏览器编辑器"导出"标记文件读写。
#
# 独立成模块（而不是像 _editor_render.json 那样各自在 webhook.py/worker.py
# 里手写一份 _read_editor_marker/_write_editor_marker）——webhook.py 的路由
# 层和 worker.py 的后台线程都要读写同一份 _editor_export.json，两边各写一份
# 容易漂移（字段改了一处没改另一处）。新功能就该从一开始只有一份实现，不是
# 又添一对手写副本。
#
# 单独的标记文件、不跟 _editor_render.json 共用——导出是 preview.mp4 的
# ffmpeg 转码，跟"保存草稿触发 Remotion 重渲染"是两条完全独立的状态机，没有
# 必要绑在一起：导出中途也该能继续保存，保存中途也该能查询上一次导出的
# 缓存结果。两者唯一的交叉点（保存正在渲染时不能开始导出，见
# webhook.py 的 export_endpoint）显式读一次 _editor_render.json 的
# state 就够，不需要合并成一份文件。

from __future__ import annotations

import json

_EDITOR_EXPORT_MARKER_NAME = "_editor_export.json"


def _default_export_marker() -> dict:
    return {
        "state": "idle",  # idle | encoding | done | failed
        "pending": None,  # {"resolution", "quality"} 排队中/正在编码的那一档
        "current": None,  # 排队中/正在编码/最近一次完成的那一档，供 /export/status 展示
        "progress": 0,  # 0-100，-progress pipe:1 解析出来的百分比
        "started_at": None,
        "error": None,
        "export_timestamps": [],  # 每小时配额独立计数，不跟 save_timestamps 混用
    }


def read_export_marker(job_dir) -> dict:
    path = job_dir / _EDITOR_EXPORT_MARKER_NAME
    default = _default_export_marker()
    if not path.exists():
        return default
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        return {**default, **marker}
    except Exception:
        return default


def write_export_marker(job_dir, marker: dict) -> None:
    (job_dir / _EDITOR_EXPORT_MARKER_NAME).write_text(
        json.dumps(marker, ensure_ascii=False), encoding="utf-8")
