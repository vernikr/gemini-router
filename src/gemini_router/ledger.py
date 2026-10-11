"""Persistent multi-process quota ledger (SPEC §3, docs/03 §3).

Single source of truth: budgets are SQL queries over `calls`, never in-memory counters
(D1). WAL + busy_timeout make daemon, stdio sessions and cron jobs share one file (D5).
Accounting rules (D3, FR-C2/C7):
- RPD (PT-day + rolling-24h dual gate) counts kind != 'count' with status in RPD_STATUSES,
  plus 'planned' rows younger than planned_ttl_s: an in-flight request is spend, so
  callers sharing this file cannot all pass the same check and overshoot the day.
- RPM/TPM sliding windows count WINDOW_STATUSES; 'planned' rows expire after planned_ttl_s.
- Provider calls that never draw on model quotas are named once in NON_SPENDING_KINDS and
  excluded from every gate: countTokens ('count') and Files API uploads ('upload').
- TPM pre-flight writes a 'planned' row (upgraded in place after the call) to close the
  countTokens→send race between processes (D2).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

RPD_STATUSES = ("ok", "quota", "http5xx", "blocked")            # conservative: 429 counts as spent
WINDOW_STATUSES = RPD_STATUSES + ("planned",)
NON_SPENDING_KINDS = ("count", "upload")     # provider calls that never draw on model quotas
SPENDS = "kind NOT IN ({})".format(",".join(f"'{k}'" for k in NON_SPENDING_KINDS))
# An upload belongs to a key, not a model, but the column is NOT NULL and feeds per-model
# filters. A sentinel no model id can equal.
FILE_ROW_MODEL = "(file)"
SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL, ts_iso TEXT NOT NULL,
  request_id TEXT, source TEXT NOT NULL DEFAULT 'cli', kind TEXT NOT NULL DEFAULT 'single',
  key_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  http_status INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  quota_error TEXT, retry_after_s REAL, latency_ms INTEGER,
  overflow_tier INTEGER NOT NULL DEFAULT 0, bytes INTEGER);
CREATE INDEX IF NOT EXISTS idx_calls_model_ts ON calls(model, ts);
CREATE INDEX IF NOT EXISTS idx_calls_key_model_ts ON calls(key_id, model, ts);
CREATE INDEX IF NOT EXISTS idx_calls_req ON calls(request_id);
CREATE TABLE IF NOT EXISTS cooldowns(
  key_id TEXT NOT NULL, model TEXT NOT NULL, until_ts REAL NOT NULL, reason TEXT,
  PRIMARY KEY(key_id, model));
CREATE TABLE IF NOT EXISTS cache(
  hash TEXT PRIMARY KEY, request_id TEXT, model TEXT NOT NULL, ts REAL NOT NULL,
  response TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
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
        """Additive only: a shared database is opened by older code too (tldr-digest),
        so an extra nullable column must never break the reader."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(calls)")}
        for col in ("bytes",):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE calls ADD COLUMN {col} INTEGER")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- calls
    def plan(self, *, key_id: str, model: str, kind: str, est_tokens: int,
             request_id: str | None, source: str, overflow_tier: int = 0,
             now: float | None = None) -> int:
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls(ts,ts_iso,request_id,source,kind,key_id,model,status,"
                "tokens_in,overflow_tier) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), request_id, source, kind, key_id, model, "planned",
                 est_tokens, overflow_tier))
            return int(cur.lastrowid)

    def resolve(self, row_id: int, status: str, *, http_status: int | None = None,
                tokens_in: int | None = None, tokens_out: int | None = None,
                latency_ms: int | None = None, quota_error: str | None = None,
                retry_after_s: float | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE calls SET status=?, http_status=COALESCE(?,http_status), "
                "tokens_in=COALESCE(?,tokens_in), tokens_out=?, latency_ms=?, quota_error=?, "
                "retry_after_s=? WHERE id=?",
                (status, http_status, tokens_in, tokens_out, latency_ms,
                 quota_error and quota_error[:500], retry_after_s, row_id))

    def record_upload(self, *, key_id: str, nbytes: int, source: str = "cli",
                      request_id: str | None = None, latency_ms: int | None = None,
                      now: float | None = None) -> int:
        """One Files API upload, recorded but never counted against model quotas: the row
        is kind='upload' with the byte count in `bytes`, and every gate skips it. It is
        the local audit trail — the provider forgets the file within 48 hours."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls(ts,ts_iso,request_id,source,kind,key_id,model,status,"
                "http_status,latency_ms,bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), request_id, source, "upload", key_id, FILE_ROW_MODEL,
                 "ok", 200, latency_ms, nbytes))
            return int(cur.lastrowid)

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
            cooldowns = {(r[0], r[1]): r[2] for r in self._conn.execute(
                "SELECT key_id, model, until_ts FROM cooldowns WHERE until_ts>?",
                (now,))}
        return {"spent": spent, "window": window, "cooldown": cooldowns}

    def last_call_ts(self, key_id: str, model: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT MAX(ts) FROM calls WHERE key_id=? AND model=? AND {SPENDS}",
                (key_id, model)).fetchone()
        return row[0]

    def calls_tail(self, n: int = 50, *, model: str | None = None, status: str | None = None,
                   source: str | None = None, request_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM calls WHERE 1=1"
        args: list = []
        for col, val in (("model", model), ("status", status), ("source", source),
                         ("request_id", request_id)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(n)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args)]

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
                    self._conn.execute("DELETE FROM cache WHERE ts<?", (cut_cache,))
                    self._conn.execute("DELETE FROM cooldowns WHERE until_ts<=?", (now,))
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
        size_mb = Path(self.path).stat().st_size / 1e6 if Path(self.path).exists() else 0
        return {"calls": calls, "cache": cache, "size_mb": round(size_mb, 2)}
