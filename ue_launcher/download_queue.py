"""Serial download/install queue for Fab library items."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .engines import EngineInstall
from .epic.fab import FabAsset
from .plugins import install_fab_content, install_fab_plugin, install_fab_project
from .projects import UProject

ProgressCb = Callable[[str, int, int], None]
QueueListener = Callable[[], None]


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class LibraryJob:
    asset: FabAsset
    engine: EngineInstall
    project: UProject | None = None
    as_project_pack: bool = False
    as_content: bool = False
    projects_root: Path = field(default_factory=lambda: Path.home() / "UnrealProjects")
    cache_dir: Path = field(default_factory=Path)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: JobState = JobState.QUEUED
    error: str | None = None
    dest: Path | None = None
    progress_msg: str = ""
    progress_pct: int | None = None

    @property
    def title(self) -> str:
        return self.asset.title

    @property
    def target_label(self) -> str:
        if self.project is not None:
            return self.project.name
        if self.as_project_pack:
            return "projects folder"
        return self.engine.path.name


class LibraryDownloadQueue:
    """One active Fab install at a time; extras wait in order."""

    def __init__(self, on_changed: QueueListener | None = None) -> None:
        self._jobs: list[LibraryJob] = []
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._on_changed = on_changed
        self._cancel_active = False

    def _notify(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    def jobs(self) -> list[LibraryJob]:
        with self._lock:
            return list(self._jobs)

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1
                for j in self._jobs
                if j.state in (JobState.QUEUED, JobState.RUNNING)
            )

    def active_job(self) -> LibraryJob | None:
        with self._lock:
            for j in self._jobs:
                if j.state == JobState.RUNNING:
                    return j
            return None

    def enqueue(self, job: LibraryJob) -> LibraryJob:
        with self._lock:
            # Skip duplicate of same asset still waiting/running.
            for existing in self._jobs:
                if (
                    existing.asset.asset_id == job.asset.asset_id
                    and existing.state in (JobState.QUEUED, JobState.RUNNING)
                ):
                    return existing
            self._jobs.append(job)
            self._trim_history_locked()
        self._notify()
        self._ensure_worker()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            for job in self._jobs:
                if job.id != job_id:
                    continue
                if job.state == JobState.QUEUED:
                    job.state = JobState.CANCELLED
                    self._notify()
                    return True
                if job.state == JobState.RUNNING:
                    # Soft cancel: finish current chunk, skip marking success.
                    self._cancel_active = True
                    return True
                return False
        return False

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = [
                j
                for j in self._jobs
                if j.state in (JobState.QUEUED, JobState.RUNNING)
            ]
        self._notify()

    def status_line(self) -> str:
        with self._lock:
            running = next((j for j in self._jobs if j.state == JobState.RUNNING), None)
            queued = sum(1 for j in self._jobs if j.state == JobState.QUEUED)
        if running is None and queued == 0:
            return ""
        if running is None:
            return f"Library queue: {queued} waiting"
        pct = f" — {running.progress_pct}%" if running.progress_pct is not None else ""
        msg = running.progress_msg or "Working…"
        tail = f" · {queued} waiting" if queued else ""
        return f"{running.title}: {msg}{pct}{tail}"

    def _trim_history_locked(self) -> None:
        finished = [
            j
            for j in self._jobs
            if j.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
        ]
        if len(finished) <= 12:
            return
        drop = {j.id for j in finished[:-12]}
        self._jobs = [j for j in self._jobs if j.id not in drop]

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_loop, daemon=True)
            self._worker.start()

    def _next_queued(self) -> LibraryJob | None:
        with self._lock:
            for job in self._jobs:
                if job.state == JobState.QUEUED:
                    job.state = JobState.RUNNING
                    job.progress_msg = "Starting…"
                    job.progress_pct = None
                    self._cancel_active = False
                    return job
        return None

    def _run_loop(self) -> None:
        while True:
            job = self._next_queued()
            if job is None:
                return
            self._notify()
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    job.state = JobState.FAILED
                    job.error = str(exc)
                    job.progress_msg = "Failed"
            self._notify()

    def _run_job(self, job: LibraryJob) -> None:
        def prog(msg: str, done: int, total: int) -> None:
            if self._cancel_active:
                return
            with self._lock:
                job.progress_msg = msg
                job.progress_pct = (
                    min(100, int(done * 100 / total)) if total else None
                )
            self._notify()

        if job.as_project_pack:
            dest = install_fab_project(
                job.asset,
                job.engine,
                job.projects_root,
                job.cache_dir,
                progress=prog,
            )
        elif job.as_content:
            if job.project is None:
                raise RuntimeError("Content install needs a project")
            dest = install_fab_content(
                job.asset,
                job.engine,
                job.project,
                job.cache_dir,
                progress=prog,
            )
        else:
            dest = install_fab_plugin(
                job.asset,
                job.engine,
                job.cache_dir,
                project=job.project,
                progress=prog,
            )

        with self._lock:
            if self._cancel_active:
                job.state = JobState.CANCELLED
                job.progress_msg = "Cancelled"
                job.dest = dest
            else:
                job.state = JobState.DONE
                job.dest = dest
                job.progress_msg = "Installed"
                job.progress_pct = 100
