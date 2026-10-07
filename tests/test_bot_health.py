import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fs25_network_core.bot_health import health_reasons, singleton_lock, write_health
from fs25_network_core.bot_supervisor import health_failure_reason


class _Task:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def _bot(now, ready=True):
    publisher = lambda: SimpleNamespace(task=_Task(), last_cycle_at=now - 5)
    return SimpleNamespace(is_ready=lambda: ready, activity_publisher=publisher(),
                           bank_notification_publisher=publisher(), server_status_publisher=publisher())


class BotHealthTests(unittest.TestCase):
    def test_gateway_and_all_publishers_must_advance(self):
        bot = _bot(1000)
        self.assertEqual(health_reasons(bot, 1000), [])
        bot.activity_publisher.last_cycle_at = 800
        self.assertIn("activity-cycle-stale", health_reasons(bot, 1000))
        bot.bank_notification_publisher.task = _Task(done=True)
        self.assertIn("bank-task-stopped", health_reasons(bot, 1000))
        bot.is_ready = lambda: False
        self.assertIn("gateway-not-ready", health_reasons(bot, 1000))

    def test_supervisor_waits_for_sustained_failure_and_rejects_old_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health.json"
            self.assertIsNone(health_failure_reason(marker, 42, 1000, 1100))
            self.assertEqual(health_failure_reason(marker, 42, 1000, 1200), "health-marker-missing")
            write_health(marker, {"pid": 42, "started_at": 1000, "updated_at": 1200,
                                  "healthy": True, "last_healthy_at": 1200})
            self.assertIsNone(health_failure_reason(marker, 42, 1000, 1210))
            self.assertEqual(health_failure_reason(marker, 43, 1000, 1210), "health-marker-missing")
            self.assertEqual(health_failure_reason(marker, 42, 1000, 1350), "health-marker-stale")

    def test_supervisor_restarts_only_after_unhealthy_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health.json"
            write_health(marker, {"pid": 42, "started_at": 1000, "updated_at": 1190,
                                  "healthy": False, "last_healthy_at": 1050,
                                  "reasons": ["activity-cycle-stale"]})
            self.assertIsNone(health_failure_reason(marker, 42, 1000, 1190))
            self.assertEqual(health_failure_reason(marker, 42, 1000, 1240),
                             "health-unhealthy:activity-cycle-stale")

    def test_second_process_owner_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            with singleton_lock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with singleton_lock(path):
                        pass
