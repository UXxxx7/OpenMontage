# database.py 并发读写回归测试——Fix C29。
#
# Run: uv run python -m whatsapp_mvp.test_database

from __future__ import annotations

import threading
import time

from sqlalchemy import text

import whatsapp_mvp.database as db
from whatsapp_mvp.job_manager import create_job, get_job, update_job_fields

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def test_wal_mode_and_busy_timeout_are_set():
    """新连接都应该带上 WAL + busy_timeout——这两条本身就是修复，不是巧合。"""
    if db._engine is None:
        db._init_engine()
    with db._engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
    check("journal_mode 是 WAL", str(mode).lower() == "wal", mode)
    check("busy_timeout 已设置（不是默认的 0）", timeout and timeout > 0, timeout)


def test_concurrent_read_write_does_not_hang():
    """Fix C29 回归测试——真实生产复现 job_f7b171f8d952：apply_style 后台管线
    线程反复写 job 状态（update_job_fields）的同时，qa_stills 的 6 个并发
    Chrome tab 反复读同一条 job 记录（/files/{job_id}/... 路由内部调用
    get_job）——默认 SQLite rollback-journal 模式在 Windows 上把整个服务器
    卡死：先是 /files 反复超时（Remotion 自己的 delayRender 28s 上限），最后
    连 /health 都完全不响应，需要手动 kill 重启整个 uvicorn 进程。

    这里用同样的并发形状压测：1 个后台写线程（模拟管线状态更新）+ 6 个并发
    读线程（模拟 Remotion 的 6 个并发 tab），验证在真正的多线程锁竞争下也
    不会卡住、不报错——不是只检查 PRAGMA 的值，是真的跑一遍会触发竞争的
    并发模式。
    """
    job = create_job(user_id=2, input_caption="C29 concurrency stress test")
    job_id = job.id

    stop = threading.Event()
    errors: list[str] = []

    def writer():
        n = 0
        while not stop.is_set():
            try:
                update_job_fields(job_id, error_message=f"stress-{n}")
                n += 1
            except Exception as e:
                errors.append(f"writer: {e}")
            time.sleep(0.02)

    max_read_time = 0.0
    lock = threading.Lock()

    def reader():
        nonlocal max_read_time
        for _ in range(30):
            if stop.is_set():
                return
            t0 = time.time()
            try:
                get_job(job_id)
            except Exception as e:
                errors.append(f"reader: {e}")
            elapsed = time.time() - t0
            with lock:
                if elapsed > max_read_time:
                    max_read_time = elapsed
            time.sleep(0.01)

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    readers = [threading.Thread(target=reader) for _ in range(6)]
    start = time.time()
    for r in readers:
        r.start()
    for r in readers:
        r.join(timeout=15)
    stop.set()
    w.join(timeout=2)
    total = time.time() - start

    check("6 个并发读线程 + 1 个后台写线程，整体在合理时间内完成（不是 28s+ 卡死）",
          total < 8.0, f"{total:.2f}s")
    check("单次读操作没有出现异常长的等待（旧行为下会卡到 28s+）",
          max_read_time < 2.0, f"{max_read_time:.3f}s")
    check("并发读写期间没有任何异常", errors == [], errors)


def main():
    test_wal_mode_and_busy_timeout_are_set()
    test_concurrent_read_write_does_not_hang()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All database concurrency tests passed.")


if __name__ == "__main__":
    main()
