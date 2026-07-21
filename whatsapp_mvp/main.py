# WhatsApp MVP - Entry Point

from __future__ import annotations

import logging
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

# 额外写一份日志到文件，便于（通过设备桥）离线查看 worker 处理过程。
# 路径可用 WA_LOG_FILE 覆盖；默认写到仓库 logs/worker.log，滚动保留最近几个。
try:
    import os as _os
    from logging.handlers import RotatingFileHandler as _RFH
    _log_path = _os.environ.get("WA_LOG_FILE") or _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "logs", "worker.log")
    _os.makedirs(_os.path.dirname(_log_path), exist_ok=True)
    _fh = _RFH(_log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as _e:  # 文件日志失败不影响主流程
    logging.getLogger(__name__).warning(f"文件日志初始化失败（忽略）: {_e}")



def main():
    """Start the FastAPI webhook server."""
    from whatsapp_mvp.webhook import app

    # Windows' default ProactorEventLoop has a confirmed bug: its one-shot
    # IOCP accept() doesn't re-arm itself after a transient OSError (seen
    # live, repeatedly: WinError 64 "The specified network name is no longer
    # available", triggered by a burst of WhatsApp webhook retries / Remotion
    # render traffic). The process survives — background pipeline threads
    # keep running — but the accept loop is dead forever, so the server
    # silently stops taking any new connection with no crash, no log past
    # "Accept failed on a socket". SelectorEventLoop's accept is poll-based
    # and doesn't share this failure mode.
    #
    # Setting asyncio.set_event_loop_policy() here does NOT work (tried it,
    # confirmed live it has zero effect): uvicorn.run()/Server.run() calls
    # asyncio.run(..., loop_factory=config.get_loop_factory()), and
    # uvicorn.loops.asyncio.asyncio_loop_factory hardcodes
    # `return asyncio.ProactorEventLoop` on win32 whenever `use_subprocess`
    # is false (uvicorn.run()'s default) — it never consults the ambient
    # event loop policy at all. The only way to actually get Selector is to
    # bypass Server.run()/uvicorn.run() and drive the server on a loop we
    # create ourselves.
    from uvicorn import Config, Server

    config = Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = Server(config)

    if sys.platform == "win32":
        import asyncio
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        logging.getLogger(__name__).info(f"event loop: {type(loop).__name__} (forced, not uvicorn default)")
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()
    else:
        server.run()


def worker():
    """Start the RQ worker."""
    import sys

    from whatsapp_mvp.config import get_config
    import rq
    from redis import Redis

    config = get_config()
    redis_conn = Redis.from_url(config.redis_url)

    # rq.Worker forks a child process per job (os.fork) to isolate it — fork()
    # doesn't exist on Windows at all, so the default Worker crashes with
    # AttributeError on the very first job it picks up (confirmed: it logged
    # the job starting, then died immediately, taking the whole worker process
    # down with it — every job after that just sat queued forever with no
    # worker left to claim it). SimpleWorker runs the job in-process instead
    # of forking; that's RQ's own documented Windows workaround. Only switch
    # on Windows — SimpleWorker skips the process-isolation forked Worker
    # gives you for free (a segfault/OOM in one job can't be contained), so
    # keep the real Worker on Linux/macOS where fork actually works.
    worker_cls = rq.SimpleWorker if sys.platform == "win32" else rq.Worker
    worker_instance = worker_cls("whatsapp_mvp", connection=redis_conn)
    worker_instance.work()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OpenMontage WhatsApp MVP")
    parser.add_argument("mode", choices=["server", "worker"], default="server", nargs="?")
    args = parser.parse_args()

    if args.mode == "worker":
        worker()
    else:
        main()
