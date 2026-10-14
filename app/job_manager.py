"""In-memory job store and lifecycle helpers."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import JobStatus


@dataclass
class Job:
    id: str
    url: str
    username: str
    password: str
    vault_ref: Optional[str]
    timeout_seconds: int
    status: JobStatus = JobStatus.RUNNING
    prompt: Optional[str] = None
    error: Optional[str] = None
    storage_state: Optional[dict[str, Any]] = None
    expires_hint: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    input_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_input: Optional[dict[str, str]] = None
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False


class JobManager:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def create(
        self,
        url: str,
        username: str,
        password: str,
        vault_ref: Optional[str],
        timeout_seconds: int,
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            url=url,
            username=username,
            password=password,
            vault_ref=vault_ref,
            timeout_seconds=timeout_seconds,
        )
        async with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        job.cancel_requested = True
        job.input_event.set()
        if job.task and not job.task.done():
            job.task.cancel()
        return True

    def submit_input(self, job: Job, otp: Optional[str], secret_answer: Optional[str]) -> None:
        payload: dict[str, str] = {}
        if otp is not None:
            payload["otp"] = otp
        if secret_answer is not None:
            payload["secret_answer"] = secret_answer
        job.pending_input = payload
        job.input_event.set()

    async def wait_for_input(self, job: Job, timeout: float) -> Optional[dict[str, str]]:
        job.input_event.clear()
        job.pending_input = None
        try:
            await asyncio.wait_for(job.input_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if job.cancel_requested:
            return None
        return job.pending_input

    async def reap_stale(self) -> None:
        now = time.time()
        async with self._lock:
            stale = [jid for jid, j in self._jobs.items() if now - j.created_at > self._ttl]
            for jid in stale:
                job = self._jobs.pop(jid, None)
                if job and job.task and not job.task.done():
                    job.cancel_requested = True
                    job.input_event.set()
                    job.task.cancel()


manager = JobManager()
