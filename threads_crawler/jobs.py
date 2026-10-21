"""Tiny background job runner: crawl và render chạy tuần tự trong luồng riêng, UI hỏi tiến độ."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


@dataclass
class Job:
    id: int
    kind: str  # "crawl" | "render"
    title: str
    status: str = "queued"  # queued | running | done | error
    progress: float = 0.0
    message: str = "Đang chờ..."
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class JobQueue:
    """One worker thread per queue: crawl không chạy chồng crawl, render không chạy chồng render."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._q: queue.Queue[tuple[Job, Callable[[ProgressFn], Any]]] = queue.Queue()
        threading.Thread(target=self._worker, name=f"jobs-{name}", daemon=True).start()

    def _worker(self) -> None:
        while True:
            job, fn = self._q.get()
            job.status = "running"

            def progress(frac: float, msg: str) -> None:
                job.progress, job.message = max(0.0, min(1.0, frac)), msg

            try:
                job.result = fn(progress)
                job.status, job.progress = "done", 1.0
            except Exception as e:
                log.error("Job %s lỗi:\n%s", job.id, traceback.format_exc())
                job.status, job.error, job.message = "error", str(e), "Lỗi"
            job.finished_at = time.time()


class JobManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}
        self.queues = {"crawl": JobQueue("crawl"), "render": JobQueue("render")}

    def submit(self, kind: str, title: str, fn: Callable[[ProgressFn], Any]) -> Job:
        job = Job(next(self._ids), kind, title)
        self.jobs[job.id] = job
        self.queues[kind]._q.put((job, fn))
        return job

    def list(self) -> list[dict]:
        return [asdict(j) for j in sorted(self.jobs.values(), key=lambda j: -j.id)][:30]

    def get(self, job_id: int) -> dict | None:
        job = self.jobs.get(job_id)
        return asdict(job) if job else None
