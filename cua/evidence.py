"""Structured evidence log (JSONL) + screenshots. Everything passes through the redactor."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .redact import Redactor


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


class RunLog:
    def __init__(self, run_dir: Path, run_id: str, redactor: Redactor) -> None:
        self.dir = run_dir
        self.run_id = run_id
        self.redactor = redactor
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screens").mkdir(exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._n = 0

    def event(self, event_kind: str, **fields: Any) -> None:
        scrubbed = self.redactor.scrub_obj(fields)
        rec = {"t": round(time.time(), 3), "run_id": self.run_id, "kind": event_kind}
        rec.update(scrubbed)
        self._events.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._events.flush()

    def screenshot_path(self, label: str) -> Path:
        self._n += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        return self.dir / "screens" / f"{self._n:03d}-{safe}.png"

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.scrub_obj(obj), indent=2, ensure_ascii=False, default=str))
        return p

    def close(self) -> None:
        self._events.close()
