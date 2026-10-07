"""Progress-based health marker for the separately supervised JiN process."""

import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger(__name__)
MAX_CYCLE_AGE = {"activity": 90, "bank": 90, "status": 180}


def runtime_dir():
    return Path(os.environ.get("FS25_JIN_RUNTIME_DIR") or
                Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "SiN" / "JiN")


@contextmanager
def singleton_lock(path):
    """Hold an OS lock for the process lifetime; refuse a second JiN owner."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"JiN is already running (lock: {target})") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def health_reasons(bot, now=None):
    """A live gateway alone is insufficient: each publisher must make progress."""
    now = time.time() if now is None else now
    reasons = []
    if not bot.is_ready():
        reasons.append("gateway-not-ready")
    for name, publisher in (("activity", bot.activity_publisher),
                            ("bank", bot.bank_notification_publisher),
                            ("status", bot.server_status_publisher)):
        task = publisher.task
        if task is None or task.done():
            reasons.append(name + "-task-stopped")
        cycle = publisher.last_cycle_at
        if cycle is None or now - cycle > MAX_CYCLE_AGE[name]:
            reasons.append(name + "-cycle-stale")
    return reasons


def write_health(path, payload):
    """Replace atomically so the supervisor never reads half-written JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(target)


class BotHealthReporter:
    def __init__(self, bot, path, interval=15):
        self.bot = bot
        self.path = path
        self.interval = interval
        self.task = None
        self.started_at = time.time()
        self.last_healthy_at = None

    def start(self):
        if self.path and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def run(self):
        while True:
            now = time.time()
            reasons = health_reasons(self.bot, now)
            if not reasons:
                self.last_healthy_at = now
            payload = {"pid": os.getpid(), "started_at": self.started_at,
                       "updated_at": now, "last_healthy_at": self.last_healthy_at,
                       "healthy": not reasons, "reasons": reasons}
            try:
                write_health(self.path, payload)
            except OSError:
                LOG.exception("[SiN JiN] health marker write failed")
            await asyncio.sleep(self.interval)
