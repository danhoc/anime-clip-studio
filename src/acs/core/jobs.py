"""Background job queue (export clips, extract frames).

Jobs run in worker threads; every ffmpeg call goes through JobContext so that it gets low
priority, progress parsing, cancellation and log capture. The queue state is persisted to
recovery/queue.json on every status change so unfinished jobs can be resumed after a crash.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, Signal

from acs.core import recovery
from acs.core.util import spawn

log = logging.getLogger("acs.jobs")

PENDING, RUNNING, DONE, FAILED, CANCELLED = "pending", "running", "done", "failed", "cancelled"
ACTIVE = (PENDING, RUNNING)


class Cancelled(Exception):
    pass


class JobError(RuntimeError):
    pass


@dataclass
class Job:
    kind: str                       # export | frames
    title: str
    spec: dict
    gpu: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    output: str = ""
    state: dict = field(default_factory=dict)   # resumable progress (e.g. frames done_until)
    created: float = field(default_factory=time.time)
    finished: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


class JobContext:
    """Handed to a runner. All helpers raise Cancelled when the job is cancelled."""

    def __init__(self, job: Job, queue: "JobQueue"):
        self.job = job
        self._queue = queue
        self._cancel = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self.settings = queue.settings

    # ---- cancellation ----
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    # ---- reporting ----
    def set_progress(self, frac: float, message: str | None = None, force: bool = False) -> None:
        self.job.progress = max(0.0, min(1.0, frac))
        if message is not None:
            self.job.message = message
        now = time.monotonic()
        if force or now - self._last_emit > 0.15:
            self._last_emit = now
            self._queue._emit_update(self.job)

    def log(self, msg: str) -> None:
        log.info("[%s] %s", self.job.id, msg)

    def warn(self, key: str) -> None:
        if key not in self.job.warnings:
            self.job.warnings.append(key)
        log.warning("[%s] warning: %s", self.job.id, key)

    def save_state(self) -> None:
        self._queue._persist()

    # ---- processes ----
    def popen(self, args: list[str], cwd: str | None = None, **kw) -> subprocess.Popen:
        self.check()
        log.debug("run: %s", subprocess.list2cmdline(args))
        p = spawn(args, cwd=cwd, **kw)
        with self._lock:
            self._procs.add(p)
        if self._cancel.is_set():
            p.kill()
        return p

    def _release(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(p)

    def run(self, args: list[str], cwd: str | None = None, duration: float | None = None,
            span: tuple[float, float] = (0.0, 1.0), message: str | None = None) -> str:
        """Run ffmpeg with `-progress pipe:1`; maps its progress into `span`. Returns stderr."""
        if message:
            self.set_progress(span[0], message, force=True)
        p = self.popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL)
        err_chunks: deque[bytes] = deque(maxlen=400)

        def read_err():
            assert p.stderr is not None
            for line in p.stderr:
                err_chunks.append(line)

        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        assert p.stdout is not None
        a, b = span
        try:
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if duration and line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    frac = min(1.0, max(0.0, us / 1e6 / duration))
                    self.set_progress(a + (b - a) * frac)
            p.wait()
        finally:
            t.join(timeout=5)
            self._release(p)
        stderr = b"".join(err_chunks).decode("utf-8", "replace")
        self.check()
        if p.returncode != 0:
            raise JobError(_tail(stderr) or f"ffmpeg exited with code {p.returncode}")
        self.set_progress(b)
        return stderr

    def capture(self, args: list[str], cwd: str | None = None, timeout: float | None = 600) -> subprocess.CompletedProcess:
        p = self.popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        finally:
            self._release(p)
        self.check()
        return subprocess.CompletedProcess(args, p.returncode, out.decode("utf-8", "replace"),
                                           err.decode("utf-8", "replace"))


_NOISE = ("analyzeduration", "Could not find codec parameters", "Consider increasing")


def clean_stderr(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if l.strip() and not any(n in l for n in _NOISE))


def _tail(text: str, n: int = 6) -> str:
    lines = clean_stderr(text).splitlines()
    return "\n".join(lines[-n:])


Runner = Callable[[JobContext, Job], None]


class JobQueue(QObject):
    job_added = Signal(str)
    job_updated = Signal(str)
    job_finished = Signal(str)
    queue_changed = Signal()

    def __init__(self, settings, runners: dict[str, Runner], parent=None,
                 post: Callable[..., None] | None = None):
        """`post(fn, *args)` (optional) runs fn on the GUI thread; used for notifications that
        originate in worker threads so no Qt signal is emitted from them."""
        super().__init__(parent)
        self.settings = settings
        self.runners = runners
        self._post = post
        self.jobs: list[Job] = []
        self._contexts: dict[str, JobContext] = {}
        self._lock = threading.RLock()
        self._closed = False   # shutdown(): nothing is started or persisted any more

    # ---- public API (main thread) ----
    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def enqueue(self, jobs: list[Job], before_start: Callable[[], None] | None = None) -> None:
        """`before_start` runs synchronously first (the pre-task project snapshot)."""
        if before_start is not None:
            before_start()
        with self._lock:
            self.jobs.extend(jobs)
        for j in jobs:
            self.job_added.emit(j.id)
        self._persist()
        self.queue_changed.emit()
        self._schedule()

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            if job.status == PENDING:
                job.status = CANCELLED
                job.finished = time.time()
                self._emit_update(job)
            ctx = self._contexts.get(job_id)
        if ctx is not None:
            ctx.cancel()
        self._persist()

    def cancel_all(self) -> None:
        for j in list(self.jobs):
            if j.status in ACTIVE:
                self.cancel(j.id)

    def shutdown(self) -> None:
        """App exit: cancel every job and forget the queue. Jobs the user cancelled this way are not
        offered for resuming at the next start (only a crash leaves an unfinished queue behind)."""
        self.cancel_all()
        with self._lock:
            self._closed = True
        try:
            recovery.save_queue_state([])
        except OSError as e:
            log.warning("cannot persist queue: %s", e)

    def retry(self, job_id: str) -> None:
        job = self.get(job_id)
        if job and job.status in (FAILED, CANCELLED):
            job.status, job.progress, job.message = PENDING, 0.0, ""
            job.warnings = []
            self._emit_update(job)
            self._persist()
            self._schedule()

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.status in ACTIVE]
        self._persist()
        self.queue_changed.emit()

    def has_active(self) -> bool:
        return any(j.status in ACTIVE for j in self.jobs)

    def restore(self, saved: list[dict]) -> list[Job]:
        """Re-add unfinished jobs from a crashed session as pending (not started)."""
        restored = []
        for d in saved:
            try:
                j = Job.from_dict(d)
            except TypeError:
                continue
            if j.status in ACTIVE:
                j.status, j.progress = PENDING, j.progress
                j.message = "resumed"
                restored.append(j)
        return restored

    # ---- internals ----
    def _notify(self, signal, *args) -> None:
        if self._post is not None and threading.current_thread() is not threading.main_thread():
            self._post(signal.emit, *args)
        else:
            signal.emit(*args)

    def _emit_update(self, job: Job) -> None:
        self._notify(self.job_updated, job.id)

    def _persist(self) -> None:
        if self._closed:
            return
        try:
            with self._lock:
                data = [j.to_dict() for j in self.jobs if j.status in ACTIVE]
            recovery.save_queue_state(data)
        except OSError as e:
            log.warning("cannot persist queue: %s", e)

    def _schedule(self) -> None:
        with self._lock:
            if self._closed:
                return
            running_cpu = sum(1 for j in self.jobs if j.status == RUNNING and not j.gpu)
            running_gpu = sum(1 for j in self.jobs if j.status == RUNNING and j.gpu)
            for job in self.jobs:
                if job.status != PENDING:
                    continue
                if job.gpu and running_gpu >= max(1, self.settings.parallel_gpu):
                    continue
                if not job.gpu and running_cpu >= max(1, self.settings.parallel_cpu):
                    continue
                job.status = RUNNING
                job.message = ""
                ctx = JobContext(job, self)
                self._contexts[job.id] = ctx
                if job.gpu:
                    running_gpu += 1
                else:
                    running_cpu += 1
                threading.Thread(target=self._run, args=(job, ctx), daemon=True,
                                 name=f"job-{job.id}").start()
                self._emit_update(job)
        self._persist()

    def _run(self, job: Job, ctx: JobContext) -> None:
        runner = self.runners.get(job.kind)
        try:
            if runner is None:
                raise JobError(f"unknown job kind {job.kind}")
            runner(ctx, job)
            job.status = DONE
            job.progress = 1.0
            if not job.message or job.message == "resumed":
                job.message = "done"
        except Cancelled:
            job.status = CANCELLED
            job.message = "cancelled"
        except Exception as e:  # noqa: BLE001 - report any failure on the job row
            log.exception("job %s failed", job.id)
            job.status = FAILED
            job.message = str(e).strip()[-600:] or e.__class__.__name__
        finally:
            job.finished = time.time()
            with self._lock:
                self._contexts.pop(job.id, None)
            self._emit_update(job)
            self._notify(self.job_finished, job.id)
            self._persist()
            self._schedule()
