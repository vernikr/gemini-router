"""Persistent multi-process quota ledger.

One SQLite WAL database is shared by every process on the machine. It holds:

* ``calls`` — one row per upstream attempt, with the full request identity
  (version, initiator, job, trace, request, hop, step) and the provider's answer;
* ``pair_state`` — the provider's own verdict for a ``(key, model)`` pair. It outranks
  any counter in ``calls``: a live 429 blocks the pair even when the counters say fresh;
* ``cooldowns`` — the compact mirror of ``pair_state`` consumed by services that read
  only that table;
* ``jobs`` / ``notes`` — the activity timeline: one row per target activity, its
  counters, and free-form annotations or lifecycle events attached to it;
* ``cache``, ``fs_affinity``, ``meta`` — response cache, per-project File Search
  ownership and schema metadata.

Accounting rules:

* RPD (PT-day + rolling 24 h dual gate) counts spending kinds with status in
  ``RPD_STATUSES`` plus fresh ``planned`` rows, so concurrent callers cannot all pass
  the same check and overshoot the day.
* RPM/TPM sliding windows count ``WINDOW_STATUSES``; ``planned`` rows expire after
  ``planned_ttl_s``.
* Provider calls that never draw on model quotas are listed in ``NON_SPENDING_KINDS``.
* A 5xx is not RPD spend: an overloaded model refused the request and consumed no
  daily slot. A 429 counts conservatively — that pair is at its limit.
* Every row is attributable to a job and a trace. ``job`` is the target activity
  (for example one digest build); ``trace`` is a single execution of it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

RPD_STATUSES = ("ok", "quota", "blocked")
WINDOW_STATUSES = RPD_STATUSES + ("planned",)
NON_SPENDING_KINDS = ("count", "upload", "drift", "filesearch")
SPENDS = "kind NOT IN ({})".format(",".join(f"'{k}'" for k in NON_SPENDING_KINDS))
# An upload belongs to a key, not a model; the column is NOT NULL and feeds per-model
# filters, so it gets a sentinel no real model can equal.
FILE_ROW_MODEL = "(file)"
SCHEMA_VERSION = "4"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL, ts_iso TEXT NOT NULL,
  request_id TEXT, source TEXT NOT NULL DEFAULT 'cli', kind TEXT NOT NULL DEFAULT 'single',
  key_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  http_status INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  quota_error TEXT, retry_after_s REAL, latency_ms INTEGER,
  overflow_tier INTEGER NOT NULL DEFAULT 0, bytes INTEGER,
  version TEXT, initiator TEXT, comment TEXT, quota_id TEXT, signal_kind TEXT,
  job TEXT, trace_id TEXT, step TEXT, hop INTEGER DEFAULT 0,
  requested_model TEXT, request_bytes INTEGER, response_bytes INTEGER,
  method TEXT, path TEXT, parent_span TEXT, provider_reason TEXT);
CREATE INDEX IF NOT EXISTS idx_calls_model_ts ON calls(model, ts);
CREATE INDEX IF NOT EXISTS idx_calls_key_model_ts ON calls(key_id, model, ts);
CREATE INDEX IF NOT EXISTS idx_calls_req ON calls(request_id);
CREATE TABLE IF NOT EXISTS cooldowns(
  key_id TEXT NOT NULL, model TEXT NOT NULL, until_ts REAL NOT NULL, reason TEXT,
  PRIMARY KEY(key_id, model));
-- Provider truth: one row per pair, upserted on every 429/503. `kind` is day
-- (hard skip until PT reset), minute (Retry-After bench, promotable to day) or
-- overload (short 503 bench).
CREATE TABLE IF NOT EXISTS pair_state(
  key_id TEXT NOT NULL, model TEXT NOT NULL,
  kind TEXT NOT NULL, until_ts REAL NOT NULL, retry_after_s REAL,
  quota_id TEXT, quota_metric TEXT, quota_value TEXT,
  reason TEXT, hits INTEGER NOT NULL DEFAULT 1,
  first_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  initiator TEXT, version TEXT, detail TEXT, job TEXT, trace_id TEXT,
  PRIMARY KEY(key_id, model));
CREATE INDEX IF NOT EXISTS idx_pair_state_until ON pair_state(until_ts);
-- Activity timeline. `event` is note | job_start | job_finish.
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL, ts_iso TEXT NOT NULL,
  version TEXT, initiator TEXT, comment TEXT NOT NULL, source TEXT,
  job TEXT, trace_id TEXT, step TEXT, event TEXT NOT NULL DEFAULT 'note');
CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts);
-- One row per target activity, with rolling counters kept in sync on every call.
CREATE TABLE IF NOT EXISTS jobs(
  job TEXT PRIMARY KEY,
  first_ts REAL NOT NULL, last_ts REAL NOT NULL, finished_ts REAL,
  initiator TEXT, trace_id TEXT, version TEXT, comment TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  calls INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
  refusals INTEGER NOT NULL DEFAULT 0, quota_429 INTEGER NOT NULL DEFAULT 0,
  models TEXT NOT NULL DEFAULT '[]', keys TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS idx_jobs_last ON jobs(last_ts);
CREATE TABLE IF NOT EXISTS cache(
  hash TEXT PRIMARY KEY, request_id TEXT, model TEXT NOT NULL, ts REAL NOT NULL,
  response TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
-- A File Search store / Files API object lives in the Google project of the key that
-- created it, so every later request must go under the SAME key. Shared storage is
-- the only place all processes can agree on that mapping.
CREATE TABLE IF NOT EXISTS fs_affinity(
  resource TEXT PRIMARY KEY,
  key_id   TEXT NOT NULL,
  ts       REAL NOT NULL);
"""


def pt_midnight_ts(tz_name: str, now: float | None = None, days_ahead: int = 0) -> float:
    tz = ZoneInfo(tz_name)
    n = datetime.now(tz) if now is None else datetime.fromtimestamp(now, tz)
    d = n.date() + timedelta(days=days_ahead)
    return datetime.combine(d, dtime.min, tzinfo=tz).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("UTC")).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)", (SCHEMA_VERSION,))

    def _migrate(self) -> None:
        """Additive only: every column is nullable, so any reader sharing this file
        keeps working. Existing rows are backfilled with derivable identity."""
        def add_columns(table: str, spec: tuple[tuple[str, str], ...]) -> None:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in spec:
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

        add_columns("calls", (
            ("bytes", "INTEGER"), ("version", "TEXT"), ("initiator", "TEXT"),
            ("comment", "TEXT"), ("quota_id", "TEXT"), ("signal_kind", "TEXT"),
            ("job", "TEXT"), ("trace_id", "TEXT"), ("step", "TEXT"),
            ("hop", "INTEGER DEFAULT 0"), ("requested_model", "TEXT"),
            ("request_bytes", "INTEGER"), ("response_bytes", "INTEGER"),
            ("method", "TEXT"), ("path", "TEXT"), ("parent_span", "TEXT"),
            ("provider_reason", "TEXT")))
        add_columns("notes", (("job", "TEXT"), ("trace_id", "TEXT"),
                              ("step", "TEXT"), ("event", "TEXT DEFAULT 'note'")))
        add_columns("pair_state", (("job", "TEXT"), ("trace_id", "TEXT")))
        self._conn.execute("UPDATE calls SET initiator=source "
                           "WHERE initiator IS NULL AND source IS NOT NULL")
        self._conn.execute("UPDATE calls SET job=(initiator || '@' || substr(ts_iso,1,10)) "
                           "WHERE job IS NULL")
        self._conn.execute("UPDATE calls SET trace_id=job WHERE trace_id IS NULL")
        self._conn.execute("UPDATE notes SET event='note' WHERE event IS NULL")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_initiator ON calls(initiator, ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_job ON calls(job, ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_trace ON calls(trace_id, ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_step ON calls(step, ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_job ON notes(job, ts)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- calls
    def plan(self, *, key_id: str, model: str, kind: str, est_tokens: int,
             request_id: str | None, source: str, overflow_tier: int = 0,
             now: float | None = None, initiator: str | None = None,
             comment: str | None = None, version: str | None = None,
             job: str | None = None, trace_id: str | None = None,
             step: str | None = None, hop: int = 0,
             requested_model: str | None = None, request_bytes: int | None = None,
             method: str | None = None, path: str | None = None,
             parent_span: str | None = None) -> int:
        """Open one upstream attempt. Its identity is the same object the log line
        uses, so a row can always be joined back to its job and trace."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls(ts,ts_iso,request_id,source,kind,key_id,model,status,"
                "tokens_in,overflow_tier,version,initiator,comment,job,trace_id,step,hop,"
                "requested_model,request_bytes,method,path,parent_span) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), request_id, source, kind, key_id, model, "planned",
                 est_tokens, overflow_tier, version, initiator or source, comment or None,
                 job, trace_id or job, step, hop, requested_model, request_bytes,
                 method, path, parent_span))
            row_id = int(cur.lastrowid)
            self._job_touch(job, initiator=initiator, version=version,
                            trace_id=trace_id or job, comment=comment,
                            model=model, key=key_id, now=now)
            return row_id

    def resolve(self, row_id: int, status: str, *, http_status: int | None = None,
                tokens_in: int | None = None, tokens_out: int | None = None,
                latency_ms: int | None = None, quota_error: str | None = None,
                retry_after_s: float | None = None, quota_id: str | None = None,
                signal_kind: str | None = None, response_bytes: int | None = None,
                provider_reason: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE calls SET status=?, http_status=COALESCE(?,http_status), "
                "tokens_in=COALESCE(?,tokens_in), tokens_out=?, latency_ms=?, quota_error=?, "
                "retry_after_s=?, quota_id=COALESCE(?,quota_id), "
                "signal_kind=COALESCE(?,signal_kind), response_bytes=?, "
                "provider_reason=COALESCE(?,provider_reason) WHERE id=?",
                (status, http_status, tokens_in, tokens_out, latency_ms,
                 quota_error and quota_error[:500], retry_after_s, quota_id,
                 signal_kind, response_bytes, provider_reason, row_id))
            row = self._conn.execute(
                "SELECT job, status FROM calls WHERE id=?", (row_id,)).fetchone()
            if row and row["job"] and status != "planned":
                self._job_count(row["job"], status, http_status)

    def record_upload(self, *, key_id: str, nbytes: int, source: str = "cli",
                      request_id: str | None = None, latency_ms: int | None = None,
                      now: float | None = None, initiator: str | None = None,
                      comment: str | None = None, version: str | None = None,
                      job: str | None = None, trace_id: str | None = None,
                      step: str | None = None) -> int:
        """One Files API upload, recorded but never counted against model quotas: the row
        is kind='upload' with the byte count in `bytes`, and every gate skips it. It is
        the local audit trail — the provider forgets the file within 48 hours."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls(ts,ts_iso,request_id,source,kind,key_id,model,status,"
                "http_status,latency_ms,bytes,version,initiator,comment,job,trace_id,step) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), request_id, source, "upload", key_id, FILE_ROW_MODEL,
                 "ok", 200, latency_ms, nbytes, version, initiator or source, comment,
                 job, trace_id or job, step))
            return int(cur.lastrowid)

    def record_drift(self, *, key_id: str, model: str, media_cls: str, level: str,
                     predicted: int, measured: int, source: str = "cli",
                     now: float | None = None) -> int:
        """A tariff forecast missed: kind='drift', counted by no gate, so honesty about
        the table costs nothing. class/level/predicted ride in quota_error as JSON."""
        now = now or time.time()
        detail = json.dumps({"class": media_cls, "level": level, "predicted": predicted})
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls(ts,ts_iso,source,kind,key_id,model,status,tokens_in,"
                "quota_error) VALUES(?,?,?,?,?,?,?,?,?)",
                (now, iso(now), source, "drift", key_id, model, "drift", measured, detail))
            return int(cur.lastrowid)

    def drift_recent(self, hours: float = 24, now: float | None = None) -> list[dict]:
        """Newest first; the doctor/quota one-line drift signal reads this."""
        since = (now or time.time()) - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, key_id, model, tokens_in, quota_error FROM calls "
                "WHERE kind='drift' AND ts>=? ORDER BY ts DESC", (since,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["quota_error"] or "{}")
            out.append({"ts": r["ts"], "key_id": r["key_id"], "model": r["model"],
                        "measured": r["tokens_in"], **d})
        return out

    def rpd_spent(self, key_id: str, model: str, day_start: float,
                  roll_start: float, planned_min: float | None = None) -> tuple[int, int]:
        """Daily and rolling spend. A fresh `planned` row counts as spent: the request is
        already on its way, so leaving it out lets concurrent callers sharing this
        database all pass the check and overshoot the day. Rows older than `planned_min`
        are abandoned attempts that never reached the provider."""
        since = min(day_start, roll_start)
        spent = "(status IN ({})".format(",".join("?" * len(RPD_STATUSES)))
        spent += (" OR (status='planned' AND ts>=?))" if planned_min is not None else ")")
        q = ("SELECT SUM(ts>=?), SUM(ts>=?) FROM calls WHERE key_id=? AND model=? "
             f"AND {SPENDS} AND ts>=? AND " + spent)
        params = [day_start, roll_start, key_id, model, since, *RPD_STATUSES]
        if planned_min is not None:
            params.append(planned_min)
        with self._lock:
            row = self._conn.execute(q, params).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def window_usage(self, key_id: str, model: str, win_start: float,
                     planned_min: float) -> tuple[int, int, float | None]:
        """→ (calls, tokens, oldest_ts) inside the sliding window (planned rows younger
        than planned_min only)."""
        q = ("SELECT COUNT(*), COALESCE(SUM(tokens_in),0), MIN(ts) FROM calls "
             f"WHERE key_id=? AND model=? AND {SPENDS} AND ts>=? AND "
             "(status IN ({}) OR (status='planned' AND ts>=?))").format(
            ",".join("?" * len(RPD_STATUSES)))
        with self._lock:
            row = self._conn.execute(
                q, (key_id, model, win_start, *RPD_STATUSES, planned_min)).fetchone()
        return int(row[0]), int(row[1]), row[2]

    def gate_snapshot(self, *, day_start: float, roll_start: float, win_start: float,
                      planned_min: float, now: float) -> dict:
        """Every gate input for every key×model pair, in three queries.

        The per-pair accessors cost a round trip each, and the budget tables ask for
        them once per pair — so a table over sixty pairs meant 180 queries on a path
        that runs per tool call and per chunk. Pairs with no history are simply
        absent; callers fall back to zeros.
        """
        rpd_q = (f"SELECT key_id, model, SUM(ts>=?), SUM(ts>=?) FROM calls "
                 f"WHERE {SPENDS} AND ts>=? AND (status IN ({{}}) OR "
                 "(status='planned' AND ts>=?)) GROUP BY key_id, model").format(
            ",".join("?" * len(RPD_STATUSES)))
        win_q = (f"SELECT key_id, model, COUNT(*), COALESCE(SUM(tokens_in),0), MIN(ts) "
                 f"FROM calls WHERE {SPENDS} AND ts>=? AND (status IN ({{}}) OR "
                 "(status='planned' AND ts>=?)) GROUP BY key_id, model").format(
            ",".join("?" * len(RPD_STATUSES)))
        since = min(day_start, roll_start)
        with self._lock:
            spent = {(r[0], r[1]): (int(r[2] or 0), int(r[3] or 0))
                     for r in self._conn.execute(
                         rpd_q, (day_start, roll_start, since, *RPD_STATUSES,
                                 planned_min))}
            window = {(r[0], r[1]): (int(r[2]), int(r[3]), r[4])
                      for r in self._conn.execute(
                          win_q, (win_start, *RPD_STATUSES, planned_min))}
            cd_rows = self._conn.execute(
                "SELECT key_id, model, until_ts, reason FROM cooldowns WHERE until_ts>?",
                (now,)).fetchall()
            cooldowns = {(r["key_id"], r["model"]): r["until_ts"] for r in cd_rows}
            pair_state = {(r["key_id"], r["model"]): dict(r) for r in self._conn.execute(
                "SELECT * FROM pair_state WHERE until_ts>?", (now,))}
            for r in cd_rows:
                if not (r["reason"] or "").startswith("rpd_429"):
                    continue
                key = (r["key_id"], r["model"])
                if key not in pair_state or pair_state[key]["kind"] != "day":
                    pair_state[key] = {
                        "key_id": r["key_id"], "model": r["model"], "kind": "day",
                        "until_ts": r["until_ts"], "reason": r["reason"],
                        "hits": 1, "initiator": "cooldowns", "version": "-"}
        return {"spent": spent, "window": window, "cooldown": cooldowns,
                "pair_state": pair_state}

    def last_call_ts(self, key_id: str, model: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT MAX(ts) FROM calls WHERE key_id=? AND model=? AND {SPENDS}",
                (key_id, model)).fetchone()
        return row[0]

    def calls_tail(self, n: int = 50, *, model: str | None = None, status: str | None = None,
                   source: str | None = None, request_id: str | None = None,
                   initiator: str | None = None, job: str | None = None,
                   trace_id: str | None = None, step: str | None = None) -> list[dict]:
        q = "SELECT * FROM calls WHERE 1=1"
        args: list = []
        for col, val in (("model", model), ("status", status), ("source", source),
                         ("request_id", request_id), ("initiator", initiator),
                         ("job", job), ("trace_id", trace_id), ("step", step)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(n)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args)]

    # ------------------------------------------------------------- provider truth
    def provider_state(self, key_id: str, model: str,
                       now: float | None = None) -> dict | None:
        """Live provider verdict for one pair, or None. Pure read (hot path).

        Some consumers write only the compact `cooldowns` view. A `rpd_429` row there
        is the same day verdict, so it is read back as provider truth.
        """
        now = now or time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pair_state WHERE key_id=? AND model=? AND until_ts>?",
                (key_id, model, now)).fetchone()
            if row:
                return dict(row)
            legacy = self._conn.execute(
                "SELECT until_ts, reason FROM cooldowns WHERE key_id=? AND model=? "
                "AND until_ts>? AND reason LIKE 'rpd_429%'", (key_id, model, now)).fetchone()
        if not legacy:
            return None
        return {"key_id": key_id, "model": model, "kind": "day",
                "until_ts": legacy["until_ts"], "reason": legacy["reason"],
                "hits": 1, "initiator": "cooldowns", "version": "-"}

    def set_pair_state(self, row: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pair_state(key_id,model,kind,until_ts,retry_after_s,quota_id,"
                "quota_metric,quota_value,reason,hits,first_ts,updated_ts,initiator,"
                "version,detail,job,trace_id) "
                "VALUES(:key_id,:model,:kind,:until_ts,:retry_after_s,:quota_id,:quota_metric,"
                ":quota_value,:reason,:hits,:first_ts,:updated_ts,:initiator,:version,:detail,"
                ":job,:trace_id) "
                "ON CONFLICT(key_id,model) DO UPDATE SET "
                "kind=excluded.kind, until_ts=excluded.until_ts, "
                "retry_after_s=excluded.retry_after_s, quota_id=excluded.quota_id, "
                "quota_metric=excluded.quota_metric, quota_value=excluded.quota_value, "
                "reason=excluded.reason, hits=excluded.hits, first_ts=excluded.first_ts, "
                "updated_ts=excluded.updated_ts, initiator=excluded.initiator, "
                "version=excluded.version, detail=excluded.detail, job=excluded.job, "
                "trace_id=excluded.trace_id", row)

    def clear_pair_state(self, key_id: str, model: str) -> None:
        """A 2xx proves the pair is usable again — drop the provider block and the
        mirrored cooldown. Truth is symmetric: refusals block, successes unblock."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pair_state WHERE key_id=? AND model=?",
                               (key_id, model))
            self._conn.execute(
                "DELETE FROM cooldowns WHERE key_id=? AND model=? AND ("
                "reason LIKE 'rpd_429%' OR reason LIKE 'rpm_429%' "
                "OR reason LIKE 'throttle_429%' OR reason LIKE 'unavailable_5%')",
                (key_id, model))

    def pair_states(self, now: float | None = None) -> dict[tuple[str, str], dict]:
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pair_state WHERE until_ts>?", (now,)).fetchall()
        return {(r["key_id"], r["model"]): dict(r) for r in rows}

    # --------------------------------------------------------------- annotations
    def add_note(self, comment: str, *, initiator: str = "", version: str = "",
                 source: str = "cli", job: str | None = None,
                 trace_id: str | None = None, step: str | None = None,
                 event: str = "note", now: float | None = None) -> int:
        """A free-form annotation or job lifecycle event. No quota effect, ever."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO notes(ts,ts_iso,version,initiator,comment,source,job,"
                "trace_id,step,event) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), version, initiator, comment[:2000], source, job,
                 trace_id or job, step, event))
            return int(cur.lastrowid)

    def notes_tail(self, n: int = 20, *, job: str | None = None) -> list[dict]:
        with self._lock:
            if job:
                rows = self._conn.execute(
                    "SELECT * FROM notes WHERE job=? ORDER BY ts DESC LIMIT ?",
                    (job, n)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM notes ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- jobs
    def _job_touch(self, job: str | None, *, initiator: str | None, version: str | None,
                   trace_id: str | None, comment: str | None, model: str,
                   key: str, now: float) -> None:
        """Create or refresh the activity row for a call. Called inside a transaction."""
        if not job:
            return
        row = self._conn.execute('SELECT models, "keys" FROM jobs WHERE job=?',
                                 (job,)).fetchone()
        models = set(json.loads(row["models"] or "[]")) if row else set()
        keys = set(json.loads(row["keys"] or "[]")) if row else set()
        models.add(model)
        keys.add(key)
        self._conn.execute(
            "INSERT INTO jobs(job,first_ts,last_ts,initiator,trace_id,version,comment,"
            "status,calls,models,\"keys\") VALUES(?,?,?,?,?,?,?,'running',1,?,?) "
            "ON CONFLICT(job) DO UPDATE SET last_ts=excluded.last_ts, "
            "trace_id=COALESCE(excluded.trace_id,trace_id), "
            "models=excluded.models, \"keys\"=excluded.\"keys\", "
            "calls=calls+1",
            (job, now, now, initiator or "", trace_id or job, version or "",
             comment or "", json.dumps(sorted(models)), json.dumps(sorted(keys))))

    def _job_count(self, job: str, status: str, http_status: int | None) -> None:
        ok = int(status == "ok")
        refusal = int(status in ("quota", "http5xx", "http4xx", "blocked", "network"))
        was_429 = int(http_status == 429)
        self._conn.execute(
            "UPDATE jobs SET ok=ok+?, refusals=refusals+?, quota_429=quota_429+? "
            "WHERE job=?", (ok, refusal, was_429, job))

    def job_start(self, job: str, *, initiator: str = "", version: str = "",
                  comment: str = "", trace_id: str | None = None,
                  step: str | None = None, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(job,first_ts,last_ts,initiator,trace_id,version,comment,"
                "status) VALUES(?,?,?,?,?,?,?,'running') "
                "ON CONFLICT(job) DO UPDATE SET status='running', "
                "finished_ts=NULL, last_ts=excluded.last_ts, "
                "trace_id=COALESCE(excluded.trace_id,trace_id), "
                "comment=COALESCE(NULLIF(excluded.comment,''),comment)",
                (job, now, now, initiator, trace_id or job, version, comment))
            self._conn.execute(
                "INSERT INTO notes(ts,ts_iso,version,initiator,comment,source,job,"
                "trace_id,step,event) VALUES(?,?,?,?,?,'api',?,?,?,'job_start')",
                (now, iso(now), version, initiator, comment or "job started", job,
                 trace_id or job, step))
        return self.job_summary(job)

    def job_finish(self, job: str, *, status: str = "done", initiator: str = "",
                   version: str = "", comment: str = "", step: str | None = None,
                   now: float | None = None) -> dict:
        if status not in ("done", "failed"):
            raise ValueError("status must be done or failed")
        now = now or time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(job,first_ts,last_ts,finished_ts,status) "
                "VALUES(?,?,?,?,?) ON CONFLICT(job) DO UPDATE SET "
                "status=excluded.status, finished_ts=excluded.finished_ts, "
                "last_ts=excluded.last_ts", (job, now, now, now, status))
            self._conn.execute(
                "INSERT INTO notes(ts,ts_iso,version,initiator,comment,source,job,"
                "trace_id,step,event) VALUES(?,?,?,?,?,'api',?,NULL,?,'job_finish')",
                (now, iso(now), version, initiator, comment or f"job {status}", job, step))
        return self.job_summary(job)

    def job_summary(self, job: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["models"] = json.loads(out["models"] or "[]")
            out["keys"] = json.loads(out["keys"] or "[]")
            out["traces"] = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT trace_id FROM calls WHERE job=? AND trace_id IS NOT NULL "
                "ORDER BY trace_id", (job,))]
            out["steps"] = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT step FROM calls WHERE job=? AND step IS NOT NULL "
                "ORDER BY step", (job,))]
        return out

    def jobs_tail(self, n: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY last_ts DESC LIMIT ?",
                                      (n,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["models"] = json.loads(d["models"] or "[]")
            d["keys"] = json.loads(d["keys"] or "[]")
            out.append(d)
        return out

    def job_calls(self, job: str, n: int = 200) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM calls WHERE job=? ORDER BY ts DESC LIMIT ?", (job, n))]

    # ---------------------------------------------------------------- cooldowns
    def cooldown(self, key_id: str, model: str, now: float | None = None) -> float | None:
        """Live cooldown deadline, or None. A pure read: this sits on the hot path
        (every budget check for every key×model pair), so it must not write. Expired
        rows are filtered here and reclaimed by prune()."""
        now = now or time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT until_ts FROM cooldowns WHERE key_id=? AND model=? AND until_ts>?",
                (key_id, model, now)).fetchone()
        return row[0] if row else None

    def set_cooldown(self, key_id: str, model: str, until_ts: float, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO cooldowns(key_id,model,until_ts,reason) VALUES(?,?,?,?) "
                "ON CONFLICT(key_id,model) DO UPDATE SET until_ts=MAX(until_ts,excluded.until_ts), "
                "reason=excluded.reason", (key_id, model, until_ts, reason))

    # ---------------------------------------------------------------- cache
    def cache_get(self, h: str | None, request_id: str | None, ttl_s: float,
                  now: float | None = None) -> dict | None:
        now = now or time.time()
        clauses, args = ["ts>=?"], [now - ttl_s]
        if h and request_id:
            clauses.append("(hash=? OR request_id=?)")
            args += [h, request_id]
        elif h:
            clauses.append("hash=?")
            args.append(h)
        elif request_id:
            clauses.append("request_id=?")
            args.append(request_id)
        else:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM cache WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT 1",
                args).fetchone()
        return dict(row) if row else None

    def cache_put(self, h: str, request_id: str | None, model: str, response: str,
                  tokens_in: int | None, tokens_out: int | None,
                  now: float | None = None) -> None:
        now = now or time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO cache(hash,request_id,model,ts,response,tokens_in,tokens_out) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET "
                "request_id=COALESCE(excluded.request_id,request_id), ts=excluded.ts, "
                "response=excluded.response, tokens_in=excluded.tokens_in, "
                "tokens_out=excluded.tokens_out",
                (h, request_id, model, now, response, tokens_in, tokens_out))

    # ---------------------------------------------------------------- maintenance
    def prune(self, *, audit_keep_days: float = 30, cache_ttl_s: float = 24 * 3600,
              now: float | None = None, dry: bool = False) -> dict:
        now = now or time.time()
        cut_calls = now - audit_keep_days * 86400
        cut_cache = now - cache_ttl_s
        with self._lock:
            n_calls = self._conn.execute(
                "SELECT COUNT(*) FROM calls WHERE ts<?", (cut_calls,)).fetchone()[0]
            n_cache = self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE ts<?", (cut_cache,)).fetchone()[0]
            if not dry:
                with self._conn:
                    self._conn.execute("DELETE FROM calls WHERE ts<?", (cut_calls,))
                    self._conn.execute("DELETE FROM notes WHERE ts<?", (cut_calls,))
                    self._conn.execute(
                        "DELETE FROM jobs WHERE last_ts<? AND status!='running'",
                        (cut_calls,))
                    self._conn.execute("DELETE FROM cache WHERE ts<?", (cut_cache,))
                    self._conn.execute("DELETE FROM cooldowns WHERE until_ts<=?", (now,))
                    self._conn.execute("DELETE FROM pair_state WHERE until_ts<=?", (now,))
        return {"calls_pruned": 0 if dry else n_calls, "cache_pruned": 0 if dry else n_cache,
                "calls_would_prune": n_calls, "cache_would_prune": n_cache}

    def vacuum(self) -> None:
        """Reclaim file space. DELETE alone never shrinks a SQLite file, so a
        retention prune that empties tables leaves a size cap unreachable.

        VACUUM cannot run inside a transaction, and under WAL its result stays in the
        write-ahead log until a checkpoint — without the TRUNCATE the main file keeps
        its old size.
        """
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def stats(self) -> dict:
        with self._lock:
            calls = self._conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            cache = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            jobs = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        size_mb = Path(self.path).stat().st_size / 1e6 if Path(self.path).exists() else 0
        return {"calls": calls, "cache": cache, "jobs": jobs,
                "size_mb": round(size_mb, 2)}

    # ------------------------------------------------- file search affinity (proxy)
    def fs_owner(self, resource: str) -> str | None:
        """The key_id that owns `resource`, or None when it has never been seen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT key_id FROM fs_affinity WHERE resource=?", (resource,)).fetchone()
        return row["key_id"] if row else None

    def fs_bind(self, resource: str, key_id: str, now: float | None = None) -> None:
        """Record (or move) the owner of a File Search resource."""
        ts = now or time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO fs_affinity(resource,key_id,ts) VALUES(?,?,?) "
                "ON CONFLICT(resource) DO UPDATE SET key_id=excluded.key_id, ts=excluded.ts",
                (resource, key_id, ts))

    def fs_forget(self, resource: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM fs_affinity WHERE resource=?", (resource,))

    def fs_affinity(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT resource, key_id FROM fs_affinity ORDER BY resource").fetchall()
        return {r["resource"]: r["key_id"] for r in rows}

    def fs_pin(self) -> str | None:
        """The File Search default project: where stores are created and where
        unscoped lists are answered from. Persisted, because two different keys would
        show two different worlds of stores (SPEC §11)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k='fs_pin'").fetchone()
        return row["v"] if row else None

    def fs_set_pin(self, key_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('fs_pin',?)", (key_id,))
