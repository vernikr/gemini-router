"""Progress event stream (SPEC §2). Frontends map events to MCP progress/logging
notifications; `waiting` events carry the countdown (FR-C13)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

STAGES = ("routing", "waiting", "pacing", "dispatch", "retry", "switch",
          "overflow", "ok", "quota_exhausted", "stream_start", "text",
          "file_progress")


@dataclass(slots=True)
class RouterEvent:
    stage: str
    model: str | None = None
    key_id: str | None = None
    wait_s: float | None = None
    pct: float | None = None
    detail: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def message(self) -> str:
        m = self.model or "?"
        d = self.detail
        if self.stage == "file_progress":
            return str(d.get("label", "Preparing audio"))
        chunk = d.get("chunk")
        chunks = d.get("chunks")
        prefix = f"{chunk}/{chunks} · " if chunk and chunks else ""
        if self.stage == "waiting":
            message = (f"waiting {self.wait_s:.0f}s for {m} "
                       f"({d.get('reason', 'gate')})")
        elif self.stage == "dispatch":
            message = f"dispatch → {m} (key {self.key_id}, est {d.get('est_tokens', '?')} tok)"
        elif self.stage == "ok":
            message = (f"ok ← {m} in={d.get('tokens_in')} "
                       f"out={d.get('tokens_out')} {d.get('latency_s')}s")
        elif self.stage == "switch":
            message = f"skip {m}: {d.get('reason', '?')}"
        elif self.stage == "overflow":
            message = f"overflow → tier {d.get('tier')}: {', '.join(d.get('models', []))}"
        else:
            message = f"{self.stage}: {m} {d or ''}".rstrip()
        return prefix + message
