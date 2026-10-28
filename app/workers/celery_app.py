from typing import Any

from celery import Celery, signals
from celery.schedules import crontab
from kombu import Queue

import app.db.models  # noqa: F401 - register every mapper so FK targets resolve in worker processes
from app.core.config import get_settings
from app.core.logging import configure_logging

QUEUES = ("default", "ingest", "ai_discovery", "ai_enrich", "reports")

_settings = get_settings()

celery_app = Celery("masteratlas", broker=_settings.broker_url, backend=_settings.celery_result_backend)
celery_app.conf.update(
    task_queues=[Queue(q) for q in QUEUES],
    task_default_queue="default",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,  # redeliver if a worker dies mid-task; tasks are idempotent on job status
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,  # results live in the jobs table
    broker_connection_retry_on_startup=True,
    # Fail fast when the broker is down so the API marks the job failed instead of hanging the request.
    broker_connection_timeout=3,
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.5,
        "interval_max": 1,
    },
    timezone="UTC",
    beat_schedule={
        "audit-partitions": {
            "task": "maintenance.ensure_audit_partitions",
            "schedule": crontab(minute=0, hour=3, day_of_month="1,15"),
        },
        "purge-expired-tokens": {
            "task": "maintenance.purge_expired_tokens",
            "schedule": crontab(minute=30, hour=3),
        },
    },
)


@signals.setup_logging.connect
def _setup_logging(**_: Any) -> None:
    # Connecting this signal stops Celery from hijacking the root logger; workers log like the API.
    configure_logging(_settings.log_level, _settings.log_json)


celery_app.autodiscover_tasks(["app.workers"], related_name="tasks", force=True)
