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
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = "Đang chờ..."
    result: Any = None
    error: str | None = None
    detail: list[str] = field(default_factory=list)  # nội dung đầy đủ: từ khóa, tùy chọn...
    log: list[list] = field(default_factory=list)  # [[thời điểm, thông báo]], các bước đã qua
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False

    def note(self, msg: str) -> None:
        self.message = msg
        if not self.log or self.log[-1][1] != msg:
            self.log.append([time.time(), msg])
            del self.log[:-200]


class JobCancelled(Exception):
    """Ném ra từ hàm báo tiến độ khi người dùng hủy tác vụ đang chạy."""


class JobQueue:
    """One worker thread per queue: crawl không chạy chồng crawl, render không chạy chồng render."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._q: queue.Queue[tuple[Job, Callable[[ProgressFn], Any]]] = queue.Queue()
        threading.Thread(target=self._worker, name=f"jobs-{name}", daemon=True).start()

    def _worker(self) -> None:
        while True:
            job, fn = self._q.get()
            if job.cancel_requested:  # hủy khi còn đang chờ
                continue
            job.status, job.started_at = "running", time.time()
            job.note("Bắt đầu")

            def progress(frac: float, msg: str, job: Job = job) -> None:
                # Tác vụ dừng ở lần báo tiến độ kế tiếp (crawl: giữa hai bài, render: giữa hai bước).
                if job.cancel_requested:
                    raise JobCancelled
                job.progress = max(0.0, min(1.0, frac))
                job.note(msg)

            try:
                job.result = fn(progress)
                job.status, job.progress = "done", 1.0
                job.note("Xong")
            except JobCancelled:
                job.status = "cancelled"
                job.note("Đã dừng")
            except Exception as e:
                log.error("Job %s lỗi:\n%s", job.id, traceback.format_exc())
                job.status, job.error = "error", str(e)
                job.note("Lỗi")
            job.finished_at = time.time()


class JobManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}
        self.queues = {"crawl": JobQueue("crawl"), "render": JobQueue("render")}

    def submit(
        self, kind: str, title: str, fn: Callable[[ProgressFn], Any], detail: list[str] | None = None
    ) -> Job:
        job = Job(next(self._ids), kind, title, detail=detail or [])
        self.jobs[job.id] = job
        self.queues[kind]._q.put((job, fn))
        return job

    def cancel(self, job_id: int) -> bool:
        """Hủy tác vụ đang chờ (bỏ luôn) hoặc đang chạy (dừng ở lần báo tiến độ kế tiếp)."""
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status, job.finished_at = "cancelled", time.time()
            job.note("Đã hủy khi đang chờ")
        else:
            job.note("Đang dừng... (dừng sau bước hiện tại)")
        return True

    def list(self) -> list[dict]:
        return [asdict(j) for j in sorted(self.jobs.values(), key=lambda j: -j.id)][:30]

    def get(self, job_id: int) -> dict | None:
        job = self.jobs.get(job_id)
        return asdict(job) if job else None
