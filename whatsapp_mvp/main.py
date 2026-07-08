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
