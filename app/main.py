from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import logging.handlers
import signal
import sys
from pathlib import Path
from typing import IO, Optional

from .config import ConfigManager
from .paths import DEFAULT_CONFIG_PATH, RUNTIME_DIR
from .service import AutoReplyService


def configure_logging(config_path: str) -> None:
    config = ConfigManager(config_path).get()
    level = getattr(logging, str(config["logging"].get("level", "INFO")).upper(), logging.INFO)
    log_path = Path(config["logging"]["file"]).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    rotating = logging.handlers.RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(formatter)
    root.addHandler(rotating)


def acquire_lock() -> IO[str]:
    path = RUNTIME_DIR / "daemon.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("another dws-auto-reply instance is already running") from exc
    handle.write(str(__import__("os").getpid()))
    handle.flush()
    return handle


async def async_main(config_path: str) -> bool:
    lock = acquire_lock()
    service = AutoReplyService(config_path)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, service.stop)
    try:
        await service.run()
        return service.restart_requested
    finally:
        lock.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Local DWS auto-reply daemon")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to config.yaml",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without starting DWS",
    )
    args = parser.parse_args(argv)
    try:
        ConfigManager(args.config)
        if args.check:
            print("configuration valid")
            return 0
        configure_logging(args.config)
        restart_requested = asyncio.run(async_main(args.config))
        return 75 if restart_requested else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print("fatal: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
