"""Worker contracts: immutable input, explicit leases and stable public job state."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .dto import StrictDTO

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled", "stale"]


class JobSpec(StrictDTO):
    kind: str = Field(min_length=1, max_length=64)
    queue: str = Field(default="default", min_length=1, max_length=64)
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=256)
    part_id: int | None = Field(default=None, gt=0)
    resource_generation: int = Field(ge=0)
    input_revision: int = Field(ge=0)
    payload: dict = Field(default_factory=dict)
    deduplication_key: str = Field(min_length=1, max_length=256)
    max_attempts: int = Field(default=5, ge=1, le=20)


class JobLease(StrictDTO):
    job_id: UUID
    kind: str
    queue: str
    resource_type: str
    resource_id: str
    part_id: int | None
    resource_generation: int
    input_revision: int
    payload: dict
    context: dict
    worker_id: str
    lease_token: UUID
    lease_epoch: int
    lease_expires_at: datetime
    attempt: int


class JobView(StrictDTO):
    job_id: UUID
    kind: str
    queue: str
    status: JobStatus
    resource_type: str
    resource_id: str
    part_id: int | None
    resource_generation: int
    input_revision: int
    attempt: int
    retry_count: int
    revision: int
    max_attempts: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    error_code: str | None
    result: dict | None


class CancelJob(StrictDTO):
    expected_revision: int = Field(ge=1)
    expected_attempt: int = Field(ge=0)
    expected_retry_count: int = Field(ge=0)


class RetryJob(CancelJob):
    pass
