import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Finished jobs kept in memory for their owners to read.
MAX_JOBS = 50


class JobFailure(Exception):
    """A failure whose message is already written for the user.

    `names_server` is true when the message already tells the user about the server that
    was created in Crafty, so nothing needs to be appended.
    """

    def __init__(self, message: str, names_server: bool = False):
        super().__init__(message)
        self.names_server = names_server


@dataclass
class Job:
    id: str
    owner_id: str
    title: str
    status: str = "running"  # running | succeeded | failed
    step: str = "Starting"
    done: int = 0
    total: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    server_id: str | None = None
    server_name: str | None = None

    def begin(self, step: str, total: int = 0) -> None:
        self.step, self.done, self.total = step, 0, total

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status, "step": self.step,
            "progress": {"done": self.done, "total": self.total},
            "warnings": self.warnings, "error": self.error,
            "server_id": self.server_id, "server_name": self.server_name,
        }


class JobRegistry:
    """Long-running work (such as building a server) tracked in memory per user."""

    def __init__(self):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    def start(self, owner_id: str, title: str,
              work: Callable[[Job], Awaitable[None]],
              describe_error: Callable[[Exception], str]) -> Job:
        job = Job(id=uuid.uuid4().hex, owner_id=owner_id, title=title)
        self._jobs[job.id] = job
        self._prune()

        async def run() -> None:
            try:
                await work(job)
            except asyncio.CancelledError:
                job.status = "failed"
                job.error = describe_error(
                    JobFailure("craftyComplete stopped while this job was running."))
                logger.warning("Job %s interrupted: %s", job.id, job.error)
                raise
            except Exception as exc:  # every failure ends up on the job, never lost
                if not isinstance(exc, JobFailure):
                    logger.exception("Job %s failed", job.id)
                job.status, job.error = "failed", describe_error(exc)
                if job.server_id:  # the in-memory job is the only other record of it
                    logger.warning("Job %s left server %s incomplete: %s",
                                   job.id, job.server_id, job.error)
            else:
                job.status, job.step = "succeeded", "Done"

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def running(self, owner_id: str) -> int:
        return sum(job.status == "running" and job.owner_id == owner_id
                   for job in self._jobs.values())

    def get(self, job_id: str, owner_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        return job if job and job.owner_id == owner_id else None

    def _prune(self) -> None:
        finished = [jid for jid, job in self._jobs.items() if job.status != "running"]
        while len(self._jobs) > MAX_JOBS and finished:
            del self._jobs[finished.pop(0)]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
