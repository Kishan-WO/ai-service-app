"""The job queue wiring (Day 11). No Redis is needed: the queue itself is tested live."""

import socket

from ai_service.core.config import Settings
from ai_service.services.job_queue import QUEUE_NAME, redis_settings
from ai_service.worker import WorkerSettings, ping


async def test_ping_reports_the_job_and_the_machine() -> None:
    result = await ping({"job_id": "j1", "job_try": 2}, "hello")
    assert result == {
        "message": "hello",
        "job_id": "j1",
        "job_try": 2,
        "worker_host": socket.gethostname(),
    }


def test_the_worker_listens_on_the_queue_the_app_writes_to() -> None:
    assert WorkerSettings.queue_name == QUEUE_NAME
    assert ping in WorkerSettings.functions


def test_the_health_check_can_notice_a_dead_worker_quickly() -> None:
    assert WorkerSettings.health_check_interval <= 30


def test_redis_settings_come_from_the_url() -> None:
    found = redis_settings("redis://redis:6379/3")
    assert (found.host, found.port, found.database) == ("redis", 6379, 3)


def test_the_default_url_is_the_compose_redis_seen_from_the_host() -> None:
    assert Settings.model_fields["REDIS_URL"].default == "redis://127.0.0.1:6380/0"
