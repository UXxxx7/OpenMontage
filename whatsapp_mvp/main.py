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

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


def worker():
    """Start the RQ worker."""
    from whatsapp_mvp.config import get_config
    import rq
    from redis import Redis

    config = get_config()
    redis_conn = Redis.from_url(config.redis_url)

    with rq.Connection(redis_conn):
        worker_instance = rq.Worker("whatsapp_mvp")
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
