import uuid
from typing import Protocol

from celery import Celery


class JobDispatcher(Protocol):
    def dispatch(self, task_name: str, job_id: uuid.UUID, queue: str) -> str:
        """Enqueue `task_name(job_id)`; returns the broker task id."""
        ...


class CeleryJobDispatcher:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def dispatch(self, task_name: str, job_id: uuid.UUID, queue: str) -> str:
        result = self._app.send_task(task_name, args=[str(job_id)], queue=queue)
        return str(result.id)
