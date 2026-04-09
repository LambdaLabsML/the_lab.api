"""Pydantic request schemas shared across route modules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ResourceItem(BaseModel):
    url: str
    label: str = ""


class NewIdeaRequest(BaseModel):
    parent_ids: list[int] = []
    description: str
    auto_checkout: bool = True


class SuggestIdeaRequest(BaseModel):
    description: str
    parent_ids: list[int] = []
    priority: Literal["normal", "high"] = "normal"
    resources: list[ResourceItem] = []


class AdoptRequest(BaseModel):
    agent_note: str | None = None


class NewExperimentRequest(BaseModel):
    description: str
    meta: dict | None = None
    script_content: str | None = None
    tags: list[str] = []
    auto_start: bool | None = None


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
