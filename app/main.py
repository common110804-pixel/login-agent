"""FastAPI entrypoint for the login agent."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException

from .job_manager import manager
from .login_agent import run_login_job
from .models import (
    CreateJobRequest,
    CreateJobResponse,
    JobInputRequest,
    JobResponse,
    JobStatus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Never attach handlers that might dump request bodies with passwords.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="Login Agent", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    async def reaper() -> None:
        while True:
            await asyncio.sleep(60)
            await manager.reap_stale()

    asyncio.create_task(reaper())


def _to_response(job) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        status=job.status,
        prompt=job.prompt,
        error=job.error,
        storage_state=job.storage_state if job.status == JobStatus.SUCCEEDED else None,
        expires_hint=job.expires_hint if job.status == JobStatus.SUCCEEDED else None,
    )


@app.post("/jobs", response_model=CreateJobResponse)
async def create_job(body: CreateJobRequest) -> CreateJobResponse:
    # TODO(production): if body.vault_ref, resolve username/password from your vault
    # and ignore raw password fields from the client.
    job = await manager.create(
        url=str(body.url),
        username=body.username,
        password=body.password,
        vault_ref=body.vault_ref,
        timeout_seconds=body.timeout_seconds,
    )
    job.task = asyncio.create_task(run_login_job(job, manager))
    return CreateJobResponse(job_id=job.id, status=job.status)


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_response(job)


@app.post("/jobs/{job_id}/input", response_model=JobResponse)
async def submit_input(job_id: str, body: JobInputRequest) -> JobResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in (JobStatus.NEEDS_OTP, JobStatus.NEEDS_SECRET_ANSWER):
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not waiting for input")
    if job.status == JobStatus.NEEDS_OTP and not body.otp:
        raise HTTPException(status_code=400, detail="otp required")
    if job.status == JobStatus.NEEDS_SECRET_ANSWER and not body.secret_answer:
        raise HTTPException(status_code=400, detail="secret_answer required")
    manager.submit_input(job, body.otp, body.secret_answer)
    job.status = JobStatus.RUNNING
    job.prompt = None
    return _to_response(job)


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    ok = await manager.delete(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": True}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
