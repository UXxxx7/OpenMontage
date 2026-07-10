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
