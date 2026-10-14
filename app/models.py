from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(str, Enum):
    RUNNING = "RUNNING"
    NEEDS_OTP = "NEEDS_OTP"
    NEEDS_SECRET_ANSWER = "NEEDS_SECRET_ANSWER"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class CreateJobRequest(BaseModel):
    url: HttpUrl
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    # Production: pass a vault item id and resolve secrets server-side.
    vault_ref: Optional[str] = None
    timeout_seconds: int = Field(default=180, ge=30, le=600)


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobInputRequest(BaseModel):
    otp: Optional[str] = None
    secret_answer: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    prompt: Optional[str] = None
    error: Optional[str] = None
    # Playwright storage_state (cookies + localStorage). Encrypt in production.
    storage_state: Optional[dict[str, Any]] = None
    expires_hint: Optional[str] = None
