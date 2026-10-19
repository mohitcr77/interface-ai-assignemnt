"""Capability artifact schema and replay result contract.

The artifact is the product of discovery: a typed, versioned, serializable
*capability* an AI agent can call by name. It is deliberately decoupled from
the LLM transcript (only a run id is kept as provenance).

Design notes (see REPORT.md):
* A Step says WHAT to do (`action`, `intent`) and WHERE (`target`), never HOW the
  surface implements it. `Target.candidates` is a ranked list of locators; the
  surface adapter resolves them. A desktop/legacy adapter would add strategies
  (e.g. `uia`, `ocr`, `coords`) without changing Step.
* Failure modes live in `conditions` (business outcome / recoverable / hard), so
  the replay result can distinguish "no such account" from "page crashed".
* Concrete values that equal an input param are stored as `{{param}}` templates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class Risk(str, Enum):
    READ = "read"                  # observes only
    REVERSIBLE = "reversible"      # navigation / typing, no committed state change
    IRREVERSIBLE = "irreversible"  # submits / commits state (money, accounts, deletes)


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"


class InputParam(BaseModel):
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    secret: bool = False           # never logged / persisted / shown to the LLM
    pattern: Optional[str] = None  # optional regex validation
    example: Optional[str] = None  # never set for secrets


class OutputSpec(BaseModel):
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""


class Locator(BaseModel):
    """One way to find a control. Candidates are tried in order of `rank`."""
    strategy: Literal["role", "label", "text", "css", "row_cell", "xpath"]
    # role: {role, name, exact}   label: {text}   text: {text, exact}
    # css: {selector}   xpath: {expr}
    # row_cell: {row_text, column}  -> cell `column` in the row containing row_text
    args: dict[str, Any]
    rank: int = 0                  # lower = preferred (more semantic / stable)
    note: str = ""                 # why this is (or is not) robust


class Target(BaseModel):
    description: str               # human-readable ("Log In button")
    candidates: list[Locator]


class ValueSpec(BaseModel):
    """Literal or templated value. `{{name}}` refers to an input param."""
    template: str


class Checkpoint(BaseModel):
    """A state assertion. All specified fields must hold."""
    url_pattern: Optional[str] = None   # canonical, e.g. /parabank/activity.htm?id=:id
    text_visible: Optional[str] = None  # may contain {{param}}
    selector_visible: Optional[str] = None
    description: str = ""


class ConditionKind(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"  # legit answer for the caller
    RECOVERABLE = "recoverable"            # handled in-flow (dismiss / wait / retry)
    HARD_FAILURE = "hard_failure"          # stop, surface, maybe escalate


class Detector(BaseModel):
    text_visible: Optional[str] = None     # regex, matched against visible page text
    selector_visible: Optional[str] = None
    url_regex: Optional[str] = None


class Condition(BaseModel):
    id: str
    kind: ConditionKind
    description: str = ""
    detect: Detector                       # empty detector => only raised via Step.if_missing
    # Armed only once an observed URL matches this regex (e.g. "session expired" is
    # only meaningful after login, otherwise the login form would trigger it).
    active_after_url: Optional[str] = None
    # BUSINESS_OUTCOME -> returned as result.outcome.code
    # RECOVERABLE      -> `recovery` is applied then the step is retried
    # HARD_FAILURE     -> failure.category = id; may escalate
    recovery: Optional[Literal["dismiss", "wait_retry", "reload"]] = None
    dismiss_selector: Optional[str] = None
    max_retries: int = 2
    escalate: bool = False


class Step(BaseModel):
    id: str
    intent: str
    action: Literal["navigate", "click", "type", "select", "press", "extract"]
    risk: Risk = Risk.REVERSIBLE
    target: Optional[Target] = None
    value: Optional[ValueSpec] = None      # type/select/navigate/press
    submit: bool = False                   # press Enter after typing
    extract_as: Optional[str] = None       # output name for `extract`
    transform: Optional[Literal["text", "currency", "integer"]] = None
    expect: Optional[Checkpoint] = None    # post-step checkpoint
    if_missing: Optional[str] = None       # condition id to raise if target not found
    source: Literal["agent", "human"] = "agent"


class Approval(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Confidence(BaseModel):
    runs: int = 0
    successes: int = 0
    score: float = 0.0                     # successes / runs over stability replays
    last_evaluated: Optional[str] = None


class Provenance(BaseModel):
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: str = ""
    discovery_run_id: str = ""
    agent_steps: int = 0


class AppRef(BaseModel):
    profile: str                          # e.g. "parabank"
    entry_url: str                        # canonical entry URL
    allowed_hosts: list[str] = []


class Capability(BaseModel):
    schema_version: str = SCHEMA_VERSION
    name: str                             # callable name, snake_case
    version: int = 1
    description: str
    goal: str
    app: AppRef
    status: Approval = Approval.DRAFT
    confidence: Confidence = Field(default_factory=Confidence)
    inputs: list[InputParam]
    outputs: list[OutputSpec]
    conditions: list[Condition] = []
    steps: list[Step]
    success: Checkpoint
    provenance: Provenance = Field(default_factory=Provenance)


# ---------------------------------------------------------------- replay result


class FailureCategory(str, Enum):
    LOCATOR_NOT_FOUND = "locator_not_found"
    LOCATOR_AMBIGUOUS = "locator_ambiguous"
    CHECKPOINT_FAILED = "checkpoint_failed"
    TIMEOUT = "timeout"
    NAVIGATION_ERROR = "navigation_error"
    POLICY_BLOCKED = "policy_blocked"
    INVALID_INPUT = "invalid_input"
    NOT_APPROVED = "not_approved"
    ABORTED_BY_OPERATOR = "aborted_by_operator"
    UNEXPECTED_STATE = "unexpected_state"


class Failure(BaseModel):
    step_id: Optional[str] = None
    category: str                          # FailureCategory value or a hard-failure condition id
    message: str
    expected: str = ""
    observed: str = ""
    url: str = ""
    screenshot: Optional[str] = None


class Outcome(BaseModel):
    code: str
    message: str = ""


class StepTrace(BaseModel):
    step_id: str
    intent: str
    status: Literal["ok", "recovered", "failed", "skipped", "human"]
    locator_used: Optional[str] = None
    attempts: int = 1
    detail: str = ""
    duration_ms: int = 0


class Recovery(BaseModel):
    step_id: str
    condition: str
    action: str


class ReplayResult(BaseModel):
    capability: str
    version: int
    status: Literal["success", "business_outcome", "failure"]
    outputs: dict[str, Any] = {}
    outcome: Optional[Outcome] = None
    failure: Optional[Failure] = None
    recoveries: list[Recovery] = []
    escalations: list[str] = []            # intervention request ids
    steps: list[StepTrace] = []
    run_id: str = ""
    duration_ms: int = 0
