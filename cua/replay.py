"""Deterministic replay engine. No LLM anywhere in this file.

Outcome taxonomy (what the caller gets back):
  success           -> outputs
  business_outcome  -> a legitimate answer ("invalid_credentials", "account_id_not_found")
  failure           -> hard failure with step / expected / observed / url / screenshot

Recoverable conditions (unexpected dialog, transient slowness) never reach the caller:
they are handled in-flow, bounded by `max_retries`, and recorded in `recoveries`.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .canon import substitute, templates_in, url_matches
from .escalation import ControlState, Escalator, InterventionRequest
from .evidence import RunLog, new_run_id
from .policy import Policy
from .redact import Redactor
from .schema import (Approval, Capability, Checkpoint, Condition, ConditionKind, Failure, FailureCategory,
                     Outcome, ParamType, Recovery, ReplayResult, Risk, Step, StepTrace)
from .surface import LocatorAmbiguous, LocatorNotFound, WebSurface

STEP_TIMEOUT_MS = 8000
MAX_HUMAN_RETRIES = 2

_DIALOG_JS = """() => { const d = document.createElement('div'); d.setAttribute('role','dialog');
 d.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center';
 d.innerHTML = '<div style="background:#fff;padding:24px"><p>Notice: scheduled maintenance banner</p><button>Dismiss</button></div>';
 d.querySelector('button').onclick = () => d.remove(); document.body.appendChild(d); }"""


@dataclass
class Faults:
    """Injected failures for demonstrating error handling: `dialog@step_id`, `session@step_id`."""
    dialog: set[str] = field(default_factory=set)
    session: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, specs: list[str]) -> "Faults":
        f = cls()
        for s in specs:
            kind, _, step = s.partition("@")
            if kind == "dialog":
                f.dialog.add(step)
            elif kind == "session":
                f.session.add(step)
            else:
                raise ValueError(f"unknown fault '{s}' (use dialog@<step> or session@<step>)")
        return f


class _Stop(Exception):
    """Internal control-flow: ends the run with a ReplayResult."""

    def __init__(self, result: ReplayResult) -> None:
        self.result = result


def coerce_param(cap: Capability, params: dict[str, Any]) -> dict[str, str]:
    """Validate & stringify inputs against the artifact's typed contract."""
    out: dict[str, str] = {}
    known = {p.name for p in cap.inputs}
    extra = set(params) - known
    if extra:
        raise ValueError(f"unknown input(s): {sorted(extra)}")
    for p in cap.inputs:
        if p.name not in params or params[p.name] in (None, ""):
            if p.required:
                raise ValueError(f"missing required input '{p.name}'")
            continue
        v = str(params[p.name])
        if p.type == ParamType.INTEGER and not re.fullmatch(r"-?\d+", v):
            raise ValueError(f"input '{p.name}' must be an integer")
        if p.type == ParamType.NUMBER:
            try:
                float(v)
            except ValueError:
                raise ValueError(f"input '{p.name}' must be a number") from None
        if p.pattern and not re.fullmatch(p.pattern, v):
            raise ValueError(f"input '{p.name}' does not match required pattern")
        out[p.name] = v
    return out


class Replayer:
    def __init__(self, surface: WebSurface, policy: Policy, log: RunLog, redactor: Redactor,
                 escalator: Optional[Escalator] = None, control: Optional[ControlState] = None,
                 faults: Optional[Faults] = None) -> None:
        self.s, self.policy, self.log, self.redactor = surface, policy, log, redactor
        self.escalator, self.control = escalator, control or ControlState()
        self.faults = faults or Faults()
        self._armed: set[str] = set()
        self._injected: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ public
    def run(self, cap: Capability, raw_params: dict[str, Any], allow_draft: bool = False) -> ReplayResult:
        t0 = time.time()
        run_id = self.log.run_id
        res = ReplayResult(capability=cap.name, version=cap.version, status="failure", run_id=run_id)
        self.log.event("replay_start", capability=cap.name, version=cap.version, status=cap.status.value,
                       inputs=[p.name for p in cap.inputs])
        try:
            if cap.status != Approval.APPROVED and not allow_draft:
                raise _Stop(self._fail(res, None, FailureCategory.NOT_APPROVED.value,
                                       "capability is not approved for unattended replay",
                                       expected="status=approved", observed=f"status={cap.status.value}"))
            try:
                params = coerce_param(cap, raw_params)
            except ValueError as e:
                raise _Stop(self._fail(res, None, FailureCategory.INVALID_INPUT.value, str(e)))
            for p in cap.inputs:
                if p.secret and p.name in params:
                    self.redactor.add_secret(params[p.name])
            self._run_steps(cap, params, res)
            self._check_success(cap, params, res)
            res.status = "success"
        except _Stop as stop:
            res = stop.result
        res.duration_ms = int((time.time() - t0) * 1000)
        res.escalations = list(self.escalator.requests) if self.escalator else []
        final = self.log.screenshot_path("final")
        self.s.screenshot(str(final))
        self.log.event("replay_end", status=res.status, outcome=res.outcome.code if res.outcome else None,
                       failure=res.failure.model_dump() if res.failure else None, outputs=res.outputs,
                       duration_ms=res.duration_ms)
        return res

    # ------------------------------------------------------------------ steps
    def _run_steps(self, cap: Capability, params: dict[str, str], res: ReplayResult) -> None:
        conds = {c.id: c for c in cap.conditions}
        i, human_retries = 0, 0
        while i < len(cap.steps):
            step = cap.steps[i]
            self.control.require_automation()
            t0 = time.time()
            try:
                trace = self._exec_step(cap, step, params, conds, res)
                trace.duration_ms = int((time.time() - t0) * 1000)
                res.steps.append(trace)
                i += 1
            except _StepFailure as sf:
                res.steps.append(StepTrace(step_id=step.id, intent=step.intent, status="failed",
                                           detail=sf.failure.message, duration_ms=int((time.time() - t0) * 1000)))
                # A hard failure the automation cannot recover from: route to a human if possible.
                resolution = self._escalate(cap, step, sf.failure, risk_gate=sf.risk_gate)
                if resolution is None:
                    raise _Stop(self._finish_failure(res, sf.failure)) from None
                if resolution.action == "abort":
                    f = sf.failure.model_copy(update={"message": sf.failure.message + " | operator aborted"})
                    f.category = sf.failure.category
                    raise _Stop(self._finish_failure(res, f)) from None
                res.steps.append(StepTrace(step_id=step.id, intent=f"human: {resolution.note or resolution.action}",
                                           status="human", detail=f"{len(resolution.human_actions)} human action(s)"))
                if resolution.action == "skip_step":
                    i += 1
                elif resolution.action == "finish":
                    return
                elif resolution.action == "approve":
                    self._approved = getattr(self, "_approved", set()) | {step.id}
                else:  # retry_step
                    human_retries += 1
                    if human_retries > MAX_HUMAN_RETRIES:
                        raise _Stop(self._finish_failure(res, sf.failure)) from None
            except _Business as b:
                res.status = "business_outcome"
                res.outcome = b.outcome
                self.log.event("business_outcome", step=step.id, code=b.outcome.code)
                raise _Stop(res) from None

    def _exec_step(self, cap: Capability, step: Step, params: dict[str, str], conds: dict[str, Condition],
                   res: ReplayResult) -> StepTrace:
        trace = StepTrace(step_id=step.id, intent=step.intent, status="ok")
        self.log.event("step_start", step=step.id, intent=step.intent, action=step.action, risk=step.risk.value,
                       value_template=step.value.template if step.value else None)

        # --- policy gate (allowlist, action types, risky/irreversible)
        target_url = substitute(step.value.template, params) if step.action == "navigate" and step.value else ""
        d = self.policy.check(step.action, step.risk, url=self.s.url() if self.s.url() != "about:blank" else "",
                              target_url=target_url)
        if not d.allow and step.id not in getattr(self, "_approved", set()):
            self.log.event("policy_block", step=step.id, reason=d.reason)
            raise _StepFailure(Failure(step_id=step.id, category=FailureCategory.POLICY_BLOCKED.value,
                                       message=d.reason, expected="allowed by policy", observed=d.reason,
                                       url=self.s.url()), risk_gate=step.risk == Risk.IRREVERSIBLE)

        # --- fault injection (demo/testing only)
        if step.id in self.faults.dialog and ("dialog", step.id) not in self._injected:
            self._injected.add(("dialog", step.id))
            self.s.page.evaluate(_DIALOG_JS)
            self.log.event("fault_injected", fault="dialog", step=step.id)
        if step.id in self.faults.session and ("session", step.id) not in self._injected:
            self._injected.add(("session", step.id))
            self.s._ctx.clear_cookies()
            self.log.event("fault_injected", fault="session", step=step.id)

        # --- pre-step: clear known recoverable interstitials, surface known conditions
        self._arm()
        cond = self._detect(cap)
        attempts = 0
        while cond is not None:
            self._handle_condition(cap, step, cond, res, attempts, trace)  # raises unless recoverable
            attempts += 1
            cond = self._detect(cap)
        trace.attempts = attempts + 1

        handle: Any = None
        if step.target is not None:
            try:
                r = self.s.resolve(step.target, params, STEP_TIMEOUT_MS)
                handle = r.handle
                trace.locator_used = r.description
                if r.fallback_used:
                    trace.status = "recovered"
                    trace.detail = "primary locator missed; used fallback candidate (drift signal)"
                    self.log.event("locator_fallback", step=step.id, used=r.description)
            except (LocatorNotFound, LocatorAmbiguous) as e:
                # A missing target may be a known condition (session gone) or a business answer.
                self._arm()
                cond = self._detect(cap)
                if cond is not None:
                    self._handle_condition(cap, step, cond, res, 0, trace)
                if step.if_missing and step.if_missing in conds:
                    raise _Business(Outcome(code=step.if_missing,
                                            message=conds[step.if_missing].description))
                cat = (FailureCategory.LOCATOR_AMBIGUOUS if isinstance(e, LocatorAmbiguous)
                       else FailureCategory.LOCATOR_NOT_FOUND)
                raise self._step_failure(step, cat.value, f"could not resolve target '{step.target.description}'",
                                         expected=f"unique visible match for {step.target.description}",
                                         observed="no match" if cat == FailureCategory.LOCATOR_NOT_FOUND else "multiple matches")

        # --- act
        self.control.require_automation()
        try:
            self._act(step, handle, params, res)
        except (LocatorNotFound,) as e:
            raise self._step_failure(step, FailureCategory.LOCATOR_NOT_FOUND.value, str(e)) from None
        except Exception as e:  # playwright timeouts / navigation errors
            name = type(e).__name__
            cat = FailureCategory.TIMEOUT if "Timeout" in name else FailureCategory.NAVIGATION_ERROR
            raise self._step_failure(step, cat.value, f"{name}: {str(e).splitlines()[0][:200]}") from None

        # --- wait for checkpoint OR a known condition
        self._await_expectation(cap, step, params, res, trace)
        self.log.event("step_ok", step=step.id, status=trace.status, locator=trace.locator_used, url=self.s.url())
        return trace

    def _act(self, step: Step, handle: Any, params: dict[str, str], res: ReplayResult) -> None:
        a = step.action
        if a == "navigate":
            self.s.goto(substitute(step.value.template, params))  # type: ignore[union-attr]
        elif a == "click":
            self.s.click(handle)
        elif a == "type":
            self.s.type(handle, substitute(step.value.template, params), submit=step.submit)  # type: ignore[union-attr]
        elif a == "select":
            self.s.select(handle, substitute(step.value.template, params))  # type: ignore[union-attr]
        elif a == "press":
            self.s.press(substitute(step.value.template, params))  # type: ignore[union-attr]
        elif a == "extract":
            raw = self._read_nonempty(handle)
            res.outputs[step.extract_as or step.id] = _transform(raw, step.transform)
        else:
            raise ValueError(f"unsupported action {a}")

    def _read_nonempty(self, handle: Any, timeout_s: float = 6.0) -> str:
        """Legacy pages fill values via XHR after the element exists: wait for real content."""
        deadline = time.time() + timeout_s
        while True:
            txt = self.s.read(handle)
            if txt:
                return txt
            if time.time() > deadline:
                raise LocatorNotFound("extraction target stayed empty")
            time.sleep(0.2)

    # ------------------------------------------------------------------ checkpoints & conditions
    def _checkpoint_ok(self, cp: Checkpoint, params: dict[str, str]) -> tuple[bool, str]:
        if cp.url_pattern and not url_matches(cp.url_pattern, self.s.url()):
            return False, f"url={self.s.url()} (want pattern {cp.url_pattern})"
        if cp.text_visible:
            want = substitute(cp.text_visible, params)
            if want.lower() not in self.s.body_text().lower():
                return False, f"text '{want}' not visible"
        if cp.selector_visible and not self._visible(cp.selector_visible):
            return False, f"selector '{cp.selector_visible}' not visible"
        return True, ""

    def _visible(self, selector: str) -> bool:
        for fr in self.s.page.frames:
            try:
                if fr.locator(selector).filter(visible=True).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _arm(self) -> None:
        # armed set maintained as URLs are observed
        self._armed_urls = getattr(self, "_armed_urls", set())
        self._armed_urls.add(self.s.url())

    def _detect(self, cap: Capability) -> Optional[Condition]:
        text: Optional[str] = None
        order = {ConditionKind.RECOVERABLE: 0, ConditionKind.BUSINESS_OUTCOME: 1, ConditionKind.HARD_FAILURE: 2}
        for c in sorted(cap.conditions, key=lambda c: order[c.kind]):
            d = c.detect
            if not (d.text_visible or d.selector_visible or d.url_regex):
                continue
            if c.active_after_url and not any(re.search(c.active_after_url, u) for u in self._armed_urls):
                continue
            if d.url_regex and not re.search(d.url_regex, self.s.url()):
                continue
            if d.selector_visible and not self._visible(d.selector_visible):
                continue
            if d.text_visible:
                text = text if text is not None else self.s.body_text()
                if not re.search(d.text_visible, text, re.I):
                    continue
            return c
        return None

    def _handle_condition(self, cap: Capability, step: Step, cond: Condition, res: ReplayResult,
                          attempt: int, trace: StepTrace) -> None:
        self.log.event("condition_detected", step=step.id, condition=cond.id, condition_kind=cond.kind.value)
        if cond.kind == ConditionKind.BUSINESS_OUTCOME:
            raise _Business(Outcome(code=cond.id, message=cond.description))
        if cond.kind == ConditionKind.RECOVERABLE:
            if attempt >= cond.max_retries:
                raise self._step_failure(step, FailureCategory.UNEXPECTED_STATE.value,
                                         f"recovery for '{cond.id}' exhausted after {attempt} attempts",
                                         expected="condition cleared", observed=f"'{cond.id}' still present")
            action = cond.recovery or "wait_retry"
            if action == "dismiss" and cond.dismiss_selector:
                for fr in self.s.page.frames:
                    loc = fr.locator(cond.dismiss_selector).filter(visible=True)
                    if loc.count():
                        loc.first.click()
                        break
            elif action == "reload":
                self.s.page.reload()
            else:
                time.sleep(1.0 * (attempt + 1))
            self.s.settle()
            res.recoveries.append(Recovery(step_id=step.id, condition=cond.id, action=action))
            trace.status = "recovered"
            trace.detail = f"recovered from '{cond.id}' via {action}"
            self.log.event("recovered", step=step.id, condition=cond.id, action=action)
            return
        raise self._step_failure(step, cond.id, cond.description or cond.id,
                                 expected="normal application state", observed=f"condition '{cond.id}' detected",
                                 escalate=cond.escalate)

    def _await_expectation(self, cap: Capability, step: Step, params: dict[str, str], res: ReplayResult,
                           trace: StepTrace) -> None:
        deadline = time.time() + STEP_TIMEOUT_MS / 1000
        recoveries = 0
        last = ""
        while True:
            self._arm()
            cond = self._detect(cap)
            if cond is not None:
                self._handle_condition(cap, step, cond, res, recoveries, trace)
                recoveries += 1
            if step.expect is None:
                return
            ok, last = self._checkpoint_ok(step.expect, params)
            if ok:
                return
            if time.time() > deadline:
                raise self._step_failure(step, FailureCategory.CHECKPOINT_FAILED.value,
                                         f"checkpoint not reached: {step.expect.description or 'expect'}",
                                         expected=_cp_desc(step.expect, params), observed=last)
            time.sleep(0.2)

    def _check_success(self, cap: Capability, params: dict[str, str], res: ReplayResult) -> None:
        missing = [o.name for o in cap.outputs if o.name not in res.outputs]
        if missing:
            raise _Stop(self._fail(res, None, FailureCategory.CHECKPOINT_FAILED.value,
                                   f"declared outputs not produced: {missing}",
                                   expected=f"outputs {[o.name for o in cap.outputs]}", observed=f"missing {missing}"))
        ok, why = self._checkpoint_ok(cap.success, params)
        if not ok:
            raise _Stop(self._fail(res, None, FailureCategory.CHECKPOINT_FAILED.value,
                                   "final success checkpoint failed", expected=_cp_desc(cap.success, params),
                                   observed=why))
        for o in cap.outputs:
            res.outputs[o.name] = _coerce_out(res.outputs[o.name], o.type)

    # ------------------------------------------------------------------ failures & escalation
    def _step_failure(self, step: Step, category: str, message: str, expected: str = "",
                      observed: str = "", escalate: bool = True) -> "_StepFailure":
        shot = self.log.screenshot_path(f"fail-{step.id}")
        self.s.screenshot(str(shot))
        f = Failure(step_id=step.id, category=category, message=message, expected=expected, observed=observed,
                    url=self.s.url(), screenshot=str(shot.relative_to(self.log.dir)))
        self.log.event("step_failed", step=step.id, category=category, message=message, expected=expected,
                       observed=observed, url=self.s.url())
        return _StepFailure(f, escalate=escalate)

    def _fail(self, res: ReplayResult, step: Optional[Step], category: str, message: str,
              expected: str = "", observed: str = "") -> ReplayResult:
        res.status = "failure"
        res.failure = Failure(step_id=step.id if step else None, category=category, message=message,
                              expected=expected, observed=observed, url=self.s.url() if self.s.url() else "")
        return res

    def _finish_failure(self, res: ReplayResult, f: Failure) -> ReplayResult:
        res.status = "failure"
        res.failure = f
        return res

    def _escalate(self, cap: Capability, step: Step, f: Failure, risk_gate: bool = False) -> Any:
        if self.escalator is None:
            return None
        no_escalate = {FailureCategory.INVALID_INPUT.value, FailureCategory.NOT_APPROVED.value}
        if f.category in no_escalate:
            return None
        req = InterventionRequest(
            capability=cap.name, run_id=self.log.run_id, step_id=step.id, step_intent=step.intent,
            reason=f.message, category=f.category, url=f.url,
            screenshot=str(self.log.dir / f.screenshot) if f.screenshot else None,
            page_text_excerpt=self.redactor.scrub(self.s.body_text()[:600]))
        if risk_gate:
            req.options = ["approve", "skip_step", "abort"]
        return self.escalator.escalate(self.s, req)


class _StepFailure(Exception):
    def __init__(self, failure: Failure, escalate: bool = True, risk_gate: bool = False) -> None:
        self.failure, self.escalate, self.risk_gate = failure, escalate, risk_gate


class _Business(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


def _cp_desc(cp: Checkpoint, params: dict[str, str]) -> str:
    bits = []
    if cp.url_pattern:
        bits.append(f"url~{cp.url_pattern}")
    if cp.text_visible:
        bits.append(f"text '{substitute(cp.text_visible, params)}'")
    if cp.selector_visible:
        bits.append(f"selector {cp.selector_visible}")
    return " & ".join(bits) or cp.description


def _transform(raw: str, kind: Optional[str]) -> Any:
    raw = raw.strip()
    if kind == "currency":
        return float(re.sub(r"[^\d.\-]", "", raw))
    if kind == "integer":
        return int(re.sub(r"[^\d\-]", "", raw))
    return raw


def _coerce_out(v: Any, t: ParamType) -> Any:
    try:
        if t == ParamType.INTEGER:
            return int(v)
        if t == ParamType.NUMBER:
            return float(v)
    except (TypeError, ValueError):
        pass
    return str(v) if t == ParamType.STRING else v
