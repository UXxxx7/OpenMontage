# WhatsApp MVP - 重型阶段并发闸门（单一事实来源）
#
# node 侧 WA_WORKER_CONCURRENCY>1 后，多任务的"规划"可以重叠（主要在等 LLM
# 响应），但 CPU/内存大户必须跨任务串行——单机上两个 Whisper 或两个 Chrome
# 渲染同时跑不是 2 倍慢，是 4-6 倍慢（缓存/内存带宽互踩），足以把两个任务
# 双双拖过超时（2026-07-08 与 2026-07-10 两次实测事故，症状都是"单任务 4
# 分钟、双任务 20 分钟无响应"）。
#
# 独立成模块是因为 pipeline_runner 和 qa_stills 都要用（qa_stills 的每张
# still 都是一次 Chrome 渲染，视觉复审重试后一个任务能渲 8-12 张），二者
# 互相 import 会成环。
#
# 槽位数按机器算力用环境变量调：笔记本默认各 1；8 核以上服务器可调
# OM_TRANSCRIBE_SLOTS=2 OM_RENDER_SLOTS=2。

from __future__ import annotations

import os
import threading

TRANSCRIBE_SLOTS = threading.Semaphore(int(os.getenv("OM_TRANSCRIBE_SLOTS", "1")))
RENDER_SLOTS = threading.Semaphore(int(os.getenv("OM_RENDER_SLOTS", "1")))
ENHANCE_SLOTS = threading.Semaphore(int(os.getenv("OM_ENHANCE_SLOTS", "1")))

# 渲染子进程硬超时：卡死的渲染不许永久占坑（2026-07-09 实测：一个挂起任务
# 瘫痪整条队列）。
RENDER_TIMEOUT_S = int(os.getenv("OM_RENDER_TIMEOUT_S", "1800"))

# 编辑器"导出"（preview.mp4 -> 转码，不是重新走 Remotion）专用槽位——刻意
# 不复用 RENDER_SLOTS：一次 Remotion 渲染能占那唯一的槽位到分钟级（最长
# RENDER_TIMEOUT_S=1800s），共用会让导出按钮排在陌生人的 WhatsApp 任务后面
# 等到半小时，也会让每次导出反过来卡住所有 WhatsApp 渲染。转码是 14-41s
# 量级（实测），配额也不能是无限——medium 预设在 1080x1920 下会吃满整台
# 机器，behind 一个 7 天有效期的 token 就能触发的接口不能是免费的 CPU DoS
# 入口。两个槽位 + 限制线程数，让并发导出总量大致封顶在一台机器的量级，
# 又不至于完全串行（导出是 CPU-bound 短任务，两路并发的总耗时跟串行差不多，
# 第二个槽位近乎白赚的吞吐）。
EXPORT_SLOTS = threading.Semaphore(int(os.getenv("OM_EXPORT_SLOTS", "2")))
# 明显小于 RENDER_TIMEOUT_S——一个卡死的 ffmpeg 不该占坑占到 30 分钟。
EXPORT_TIMEOUT_S = int(os.getenv("OM_EXPORT_TIMEOUT_S", "900"))
EXPORT_THREADS = int(os.getenv("OM_EXPORT_THREADS", "0")) or max(2, (os.cpu_count() or 4) // 2)
