"""Pydantic request schemas shared across route modules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    # Pin the new idea branch to an exact commit instead of branching from
    # whatever HEAD the parent/base branch happens to be at. When set, the
    # branch is created with a plain ``git branch`` (update-ref), NEVER
    # checked out anywhere — auto_checkout is ignored — so this never touches
    # the caller's worktree or leaves a "carry-over" auto-commit on it.
    # Only valid with zero or one parent (a multi-parent merge inherently
    # needs a working tree). Full commit SHA required.
    commit: str | None = Field(
        default=None,
        description=(
            "Full commit SHA to point the new idea branch at directly, "
            "without checking anything out. Mutually exclusive with multiple "
            "parent_ids; forces auto_checkout to be ignored (always False)."
        ),
    )


class SuggestIdeaRequest(BaseModel):
    description: str
    parent_ids: list[int] = []
    priority: Literal["normal", "high"] = "normal"
    resources: list[ResourceItem] = []


class AdoptRequest(BaseModel):
    agent_note: str | None = None


class ResourceRequirements(BaseModel):
    units: int | None = None      # None → use the matched resource's default
    # kind: "gpu" | "cpu" | "none" | "any" (or None) — None/any matches any
    # resource. Most experiments don't need to pin to a specific kind; vLLM
    # client experiments in particular share a host-side server and just
    # consume queue width.
    kind: str | None = None
    tags: list[str] = []


class NewExperimentRequest(BaseModel):
    # Unknown top-level fields are a 422, not a silent drop. Pydantic's
    # default (extra="ignore") is exactly how a "flat" meta — hyperparameters
    # the caller meant to nest under ``meta`` but flattened at the top level
    # instead — went missing with no error (idea 1007).
    model_config = ConfigDict(extra="forbid")

    description: str
    meta: dict | None = Field(
        default=None,
        description=(
            "Arbitrary caller-supplied config/hyperparameters for this run. "
            "Must be a JSON object (not flattened into the request body). "
            "Keys the Lab itself writes at runtime (see LAB_WRITTEN_META_KEYS) "
            "are rejected here — the Lab owns those."
        ),
    )
    script_content: str | None = Field(
        default=None, description="Full shell script body to run (``#!/bin/bash...``).")
    tags: list[str] = Field(default=[], description="Free-form labels for filtering/comparison.")
    auto_start: bool | None = None  # legacy, still accepted (ignored — queue handles it)
    priority: int = Field(default=0, description="Higher runs first among queued experiments.")
    requirements: ResourceRequirements | None = Field(
        default=None, description="Resource constraints (unit count/kind/tags) for scheduling.")
    depends_on: list[str] = Field(
        default=[], description="Experiment labels that must finish before this one is dispatched.")
    depends_on_success: bool = Field(
        default=True, description="If true, a failed dependency blocks this experiment too.")


class BatchExperimentEntry(BaseModel):
    """One experiment within a batch-create request's ``metas`` list."""
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, description="Advisory label; recorded as meta.requested_label.")
    meta: dict | None = Field(default=None, description="Per-experiment config/hyperparameters (JSON object).")
    tags: list[str] | None = None
    description: str | None = None


class BatchCreateExperimentsRequest(BaseModel):
    """Body of ``POST /ideas/{idea_id}/experiments/batch``."""
    model_config = ConfigDict(extra="forbid")

    shared_script_content: str | None = Field(
        default=None, description="Script body shared by every entry in metas.")
    metas: list[BatchExperimentEntry] = Field(
        description="One entry per experiment to create; each may override meta/tags/description.")


class CloneExperimentRequest(BaseModel):
    """Body of ``POST /experiments/clone``: create new experiment(s) that
    copy an existing run's settings and script, with overrides applied on
    top."""
    model_config = ConfigDict(extra="forbid")

    clone_of: str = Field(description="Label or id of the source experiment to clone (e.g. '1026.1').")
    overrides: dict | None = Field(
        default=None,
        description=(
            "Meta keys to change relative to the source. Applied after "
            "lab-written runtime keys are stripped from the copied meta; "
            "overrides may not set lab-written runtime keys either."
        ),
    )
    seed: int | None = Field(
        default=None,
        description="If set, written to meta.seed (incremented per clone when count>1).",
    )
    count: int = Field(default=1, ge=1, le=50, description="Number of clones to create.")
    commit: str | None = Field(
        default=None,
        description=(
            "Full commit SHA to pin the clone(s) to, instead of inheriting "
            "the source idea branch's HEAD at dispatch time."
        ),
    )
    description: str | None = Field(
        default=None, description="Description for the clone(s); defaults to 'clone of <source>'.")
    tags: list[str] | None = Field(
        default=None, description="Tags for the clone(s); defaults to the source's tags.")


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
    disable_password: str | None = None


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


class MessageRequest(BaseModel):
    to: str | None = Field(
        default=None,
        description=(
            "Recipient: 'agent:<id>' (one agent), 'role:<role>' (every agent "
            "with that role), 'all' (every agent), 'user' (the human in the "
            "dashboard), or 'thread:<id>' (the thread's members). May be "
            "omitted when thread_id or in_reply_to is given — the message "
            "then goes to that thread."
        ),
    )
    text: str = Field(
        default="",
        description=(
            "Message body. May be omitted/blank if tldr is given — the tldr "
            "then stands in as the body too."
        ),
    )
    tldr: str | None = Field(
        default=None,
        description=(
            "Optional concise, one-line summary used for inbox and event "
            "previews. Kept short (500 chars): a longer one is stored "
            "truncated at a word boundary, with a warning in the response."
        ),
    )
    in_reply_to: int | None = Field(
        default=None,
        description="Id of the message this replies to. Inherits its thread_id "
                    "unless you pass a contradictory one.",
    )
    thread_id: int | None = Field(
        default=None,
        description="Id of an existing thread (its root message's id) to post "
                    "into. Normally inferred from in_reply_to or to='thread:<id>' — "
                    "only needed on its own, e.g. {thread_id, text} with no 'to'.",
    )
    references: list[dict] = Field(
        default=[],
        description="Explicit handles this message is about, e.g. "
                    "[{'kind':'idea','id':42}, {'kind':'experiment','id':'12.3'}, "
                    "{'kind':'resource','path':'reports/x.md'}]. Mentions like "
                    "'exp 12.3' or 'idea 42' in the text are added automatically "
                    "when they exist.",
    )


class ThreadCreateRequest(BaseModel):
    title: str = Field(description="Short thread title (max 200 chars).")
    text: str = Field(description="The opening message's body.")
    tldr: str | None = Field(
        default=None,
        description="Optional one-line summary for the opening message (same "
                    "500-char soft limit as MessageRequest.tldr).",
    )
    visibility: str = Field(
        default="public",
        description="'public': every agent can find, read and join it. "
                    "'private': only 'members' (and the creator) can.",
    )
    members: list[str] = Field(
        default=[], description="Agent ids to add as members; they receive "
                                 "messages posted to this thread.",
    )
    announce: bool = Field(
        default=False,
        description="If true, send the opening message to 'all' once so every "
                    "agent is notified; otherwise others discover it via "
                    "GET /threads?q=<topic>. Only valid for public threads.",
    )
    references: list[dict] = Field(
        default=[], description="Same shape as MessageRequest.references, for "
                                 "the opening message.",
    )


class ThreadUpdateRequest(BaseModel):
    title: str | None = Field(default=None, description="New title (max 200 chars).")
    summary: str | None = Field(
        default=None,
        description="Rolling summary so newcomers can catch up without reading "
                    "every message; keep it current.",
    )
    status: str | None = Field(default=None, description="'open' or 'closed'. "
                                "Closing posts 'conclusion' (if given) to the thread.")
    conclusion: str | None = Field(
        default=None, description="Outcome text posted to the thread when closing it.")
    visibility: str | None = Field(default=None, description="'public' or 'private'.")


class ThreadMembersRequest(BaseModel):
    agent_ids: list[str] = Field(description="Agent ids to add as members of this thread.")


class RequeueRequest(BaseModel):
    reason: str = "preempted"
    job_id: str | None = None
    restart_count: int | None = None


class SlurmDoneRequest(BaseModel):
    exit_code: int = 0
    job_id: str | None = None
    restart_count: int | None = None
