"""Human-in-the-loop escalation and control transfer.

Control model
-------------
Exactly one party owns the live session at any time: AUTOMATION or HUMAN. Every
automation action must pass `ControlState.require_automation()`, so an automated
click can never race a human. Transfers are recorded (who / when / why).

Handoff protocol
----------------
1. Automation detects it is stuck (hard failure with `escalate`, risky step needing
   a decision, agent loop stuck) and builds an `InterventionRequest` carrying
   capability, step, reason, URL, screenshot and a redacted text excerpt.
2. `Escalator.escalate()` flips control to HUMAN, persists the request, and starts
   recording human actions on the SAME Playwright page (same cookies / session /
   DOM) - there is no new browser, no re-login.
3. The `Operator` (web console, or a scripted mock) lets the human act, then
   returns a `Resolution`: retry_step | skip_step | finish | abort.
4. Control flips back to AUTOMATION, human actions are written to evidence, and
   the run resumes from the resolution.

In production `Operator` is a queue consumer + a co-browsing console (the page
would be exposed through CDP screencast / VNC). Here the web operator is a tiny
local HTTP page next to a headed browser window; the scripted operator stands in
for a human in headless/CI demos. Both exercise the same real handoff code.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

from .evidence import RunLog


class Owner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class ControlState:
    def __init__(self) -> None:
        self.owner = Owner.AUTOMATION
        self.transfers: list[dict[str, Any]] = []

    def transfer(self, to: Owner, reason: str) -> None:
        self.transfers.append({"t": round(time.time(), 3), "from": self.owner.value, "to": to.value, "reason": reason})
        self.owner = to

    def require_automation(self) -> None:
        if self.owner != Owner.AUTOMATION:
            raise RuntimeError("automation attempted to act while a human owns the session")


@dataclass
class InterventionRequest:
    capability: str
    run_id: str
    step_id: Optional[str]
    step_intent: str
    reason: str
    category: str
    url: str
    screenshot: Optional[str]
    page_text_excerpt: str
    options: list[str] = field(default_factory=lambda: ["retry_step", "skip_step", "finish", "abort"])
    id: str = field(default_factory=lambda: "ir-" + uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=lambda: round(time.time(), 3))


@dataclass
class Resolution:
    action: Literal["retry_step", "skip_step", "finish", "abort", "approve"]
    note: str = ""
    human_actions: list[dict[str, Any]] = field(default_factory=list)


class Operator(Protocol):
    def handle(self, req: InterventionRequest, surface: Any) -> Resolution: ...


class NoOperator:
    """Unattended: the request is persisted (the 'queue') and the run aborts cleanly."""

    def handle(self, req: InterventionRequest, surface: Any) -> Resolution:
        return Resolution("abort", "no operator attached; intervention request persisted")


class ScriptedOperator:
    """Mock human for headless demos/tests. `script(surface, req)` drives the live page."""

    def __init__(self, script: Callable[[Any, InterventionRequest], Resolution]) -> None:
        self.script = script

    def handle(self, req: InterventionRequest, surface: Any) -> Resolution:
        return self.script(surface, req)


class WebOperator:
    """Minimal operator console. The human works in the (headed) browser window and
    clicks Resume/Skip/Finish/Abort here. Blocks until a decision arrives."""

    def __init__(self, port: int = 8765, timeout_s: int = 900) -> None:
        self.port, self.timeout_s = port, timeout_s

    def handle(self, req: InterventionRequest, surface: Any) -> Resolution:
        decision: dict[str, str] = {}
        done = threading.Event()
        shot = req.screenshot

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/resolve"):
                    action = self.path.split("action=")[-1]
                    decision["action"] = action
                    done.set()
                    self._send(200, b"Control returned to automation. You can close this tab.", "text/plain")
                elif self.path == "/shot.png" and shot and Path(shot).exists():
                    self._send(200, Path(shot).read_bytes(), "image/png")
                else:
                    body = (f"<h2>Intervention requested: {req.capability}</h2>"
                            f"<p><b>Reason:</b> {req.reason}<br><b>Step:</b> {req.step_id} - {req.step_intent}<br>"
                            f"<b>URL:</b> {req.url}</p><img src='/shot.png' width=600><pre>{req.page_text_excerpt}</pre>"
                            "<p>Take over in the browser window (same live session), then choose:</p>" +
                            "".join(f"<a href='/resolve?action={o}'><button>{o}</button></a> " for o in req.options))
                    self._send(200, body.encode(), "text/html")

            def _send(self, code: int, data: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(data)

        srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[operator] intervention {req.id}: open http://127.0.0.1:{self.port}  (reason: {req.reason})")
        done.wait(self.timeout_s)
        srv.shutdown()
        return Resolution(decision.get("action", "abort"), "web operator")  # type: ignore[arg-type]


class Escalator:
    def __init__(self, operator: Operator, control: ControlState, log: RunLog) -> None:
        self.operator, self.control, self.log = operator, control, log
        self.requests: list[str] = []

    def escalate(self, surface: Any, req: InterventionRequest) -> Resolution:
        self.requests.append(req.id)
        self.log.write_json(f"intervention-{req.id}.json", asdict(req))
        self.log.event("escalation_raised", request_id=req.id, reason=req.reason, step=req.step_id,
                       category=req.category, url=req.url)
        self.control.transfer(Owner.HUMAN, f"{req.category}: {req.reason}")
        surface.start_human_recording()
        self.log.event("control_transferred", to="human", request_id=req.id)
        try:
            res = self.operator.handle(req, surface)
        finally:
            actions = surface.stop_human_recording()
            self.control.transfer(Owner.AUTOMATION, "operator returned control")
        res.human_actions = actions or res.human_actions
        self.log.event("human_actions", request_id=req.id, actions=res.human_actions)
        self.log.event("control_returned", to="automation", request_id=req.id, resolution=res.action,
                       note=res.note, url=surface.url())
        self.log.write_json(f"handoff-{req.id}.json", {"transfers": self.control.transfers,
                                                       "resolution": res.action, "note": res.note,
                                                       "human_actions": res.human_actions})
        return res
