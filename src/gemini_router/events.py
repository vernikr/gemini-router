"""Progress event stream (SPEC §2). Frontends map events to MCP progress/logging
notifications; `waiting` events carry the countdown (FR-C13)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

STAGES = ("routing", "waiting", "pacing", "dispatch", "retry", "switch",
          "overflow", "ok", "quota_exhausted")


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
        if self.stage == "waiting":
            return (f"waiting {self.wait_s:.0f}s for {m} "
                    f"({self.detail.get('reason', 'gate')})")
        d = self.detail
        if self.stage == "dispatch":
            return f"dispatch → {m} (key {self.key_id}, est {d.get('est_tokens', '?')} tok)"
        if self.stage == "ok":
            return (f"ok ← {m} in={d.get('tokens_in')} "
                    f"out={d.get('tokens_out')} {d.get('latency_s')}s")
        if self.stage == "switch":
            return f"skip {m}: {d.get('reason', '?')}"
        if self.stage == "overflow":
            return f"overflow → tier {d.get('tier')}: {', '.join(d.get('models', []))}"
        return f"{self.stage}: {m} {self.detail or ''}".rstrip()
