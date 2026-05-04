"""Pydantic request schemas shared across route modules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ResourceItem(BaseModel):
    url: str
    label: str = ""


class NewIdeaRequest(BaseModel):
    # None  → infer parent from the currently checked-out branch
    # []    → no parent (root idea, branched from main)
    # [..]  → explicit parents
    parent_ids: list[int] | None = None
    description: str
    auto_checkout: bool = True


class SuggestIdeaRequest(BaseModel):
    description: str
    parent_ids: list[int] = []
    priority: Literal["normal", "high"] = "normal"
    resources: list[ResourceItem] = []


class AdoptRequest(BaseModel):
    agent_note: str | None = None


class ResourceRequirements(BaseModel):
    units: int | None = None      # None → use the matched resource's default
    kind: str = "gpu"              # gpu | cpu | none
    tags: list[str] = []


class NewExperimentRequest(BaseModel):
    description: str
    meta: dict | None = None
    script_content: str | None = None
    tags: list[str] = []
    auto_start: bool | None = None  # legacy, still accepted (ignored — queue handles it)
    priority: int = 0
    requirements: ResourceRequirements | None = None
    depends_on: list[str] = []
    depends_on_success: bool = True


class PriorityRequest(BaseModel):
    priority: int


class QueueConfigRequest(BaseModel):
    paused: bool | None = None
    dispatch_interval_s: float | None = None


class ResourceRequest(BaseModel):
    name: str
    kind: str = "local"
    unit_kind: str = "gpu"
    capacity: int = 1
    jobs_per_unit: float = 1.0
    tags: list[str] = []
    executor_config: dict = {}


class StartExperimentRequest(BaseModel):
    timeout: float | None = None


class NoteRequest(BaseModel):
    text: str
    level: str = "observation"
    resources: list[ResourceItem] = []


class ConcludeRequest(BaseModel):
    conclusion: str


class AbandonRequest(BaseModel):
    reason: str


class ReopenRequest(BaseModel):
    reason: str


class SandboxConfigRequest(BaseModel):
    enabled: bool = True
    allowlist: list[str] = []
    denylist: list[str] = []
    file_rw: list[str] = []
    file_ro: list[str] = []


class AnalyzeRequest(BaseModel):
    experiment_ids: list[int]
    script: str
    args: list[str] = []


class ChatRequest(BaseModel):
    messages: list[dict]


class TaskRequest(BaseModel):
    text: str


class RenameTagRequest(BaseModel):
    old: str
    new: str


class UpdateTagsRequest(BaseModel):
    add: list[str] = []
    remove: list[str] = []
