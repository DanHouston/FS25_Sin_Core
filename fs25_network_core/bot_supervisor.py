"""Start and supervise one JiN child process (run with ``python -m``)."""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT
from .bot_health import runtime_dir, singleton_lock


LOG = logging.getLogger(__name__)
POLL_SECONDS = 15
STARTUP_GRACE_SECONDS = 180
STALE_SECONDS = 120
UNHEALTHY_SECONDS = 180


def health_failure_reason(path, pid, started_at, now=None):
    """Return a restart reason only after a sustained failure."""
    now = time.time() if now is None else now
    try:
        marker = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = None
    try:
        marker_started = float(marker.get("started_at") or 0) if isinstance(marker, dict) else 0
        updated_at = float(marker.get("updated_at") or 0) if isinstance(marker, dict) else 0
        last_good = float(marker.get("last_healthy_at") or started_at) if isinstance(marker, dict) else started_at
    except (TypeError, ValueError):
        marker = None
        marker_started = updated_at = 0
        last_good = started_at
    if not isinstance(marker, dict) or marker.get("pid") != pid or \
            marker_started < started_at - 2:
        return "health-marker-missing" if now - started_at > STARTUP_GRACE_SECONDS else None
    if now - updated_at > STALE_SECONDS:
        return "health-marker-stale" if now - started_at > STARTUP_GRACE_SECONDS else None
    if marker.get("healthy") is True:
        return None
    if now - last_good > UNHEALTHY_SECONDS:
        return "health-unhealthy:" + ",".join(marker.get("reasons") or ["unknown"])
    return None


def stop_child(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def supervise(runtime):
    marker = runtime / "health.json"
    restarts = []
    with (runtime / "bot.log").open("a", encoding="utf-8") as bot_log:
        while True:
            now = time.time()
            restarts = [value for value in restarts if now - value < 1800]
            if len(restarts) >= 5:
                LOG.error("[SiN JiN] too many restarts in 30 minutes; operator action required")
                return 1
            env = os.environ.copy()
            env["FS25_JIN_HEALTH_FILE"] = str(marker)
            env["FS25_JIN_RUNTIME_DIR"] = str(runtime)
            env["PYTHONUNBUFFERED"] = "1"
            started_at = time.time()
            child = subprocess.Popen([sys.executable, "-m", "fs25_network_core.bot_frontend"],
                                     cwd=PROJECT_ROOT, env=env, stdout=bot_log, stderr=subprocess.STDOUT)
            LOG.info("[SiN JiN] started bot pid=%s", child.pid)
            reason = None
            try:
                while True:
                    time.sleep(POLL_SECONDS)
                    if child.poll() is not None:
                        reason = "process-exited:" + str(child.returncode)
                        break
                    reason = health_failure_reason(marker, child.pid, started_at)
                    if reason:
                        break
            finally:
                stop_child(child)
            LOG.error("[SiN JiN] restarting bot pid=%s reason=%s", child.pid, reason)
            restarts.append(time.time())
            time.sleep(10)


def main():
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=runtime / "supervisor.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("[SiN JiN] supervisor starting root=%s", PROJECT_ROOT)
    with singleton_lock(runtime / "supervisor.lock"):
        return supervise(runtime)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.info("[SiN JiN] supervisor stopped by operator")
