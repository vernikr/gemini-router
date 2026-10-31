"""Terminal UX (FR-C12): quota, models, calls, jobs, job, notes, prune, doctor, proxy.
Works over SSH against the shared state file without touching the daemon (FR-E7)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table

from . import __version__
from .config import Config
from .context import audit_line
from .errors import RouterError
from .events import RouterEvent
from .ledger import Ledger, iso, pt_midnight_ts
from .routing import Router, pick_gemma

console = Console()


def _emit_audit(line: str, args) -> None:
    """Keep `--json` stdout parseable; the audit line still exists on stderr."""
    print(line, file=sys.stderr if getattr(args, "json", False) else sys.stdout)


def _load_cfg(args) -> Config:
    over = {"state_path": args.state} if getattr(args, "state", None) else {}
    return Config.load(getattr(args, "config", None), **over)


def _router(cfg: Config) -> Router:
    return Router(cfg, ledger=Ledger(cfg.state_path))


def _drift_line(ledger: Ledger) -> str | None:
    """One line when the tariff table has been missing: the reminder to re-measure it."""
    misses = ledger.drift_recent()
    if not misses:
        return None
    m = misses[0]
    return (f"{len(misses)} forecast miss(es) in the last 24 h — newest: "
            f"{m.get('class', '?')}/{m.get('level', '?')} on {m['model']} "
            f"predicted {m.get('predicted', '?')}, measured {m['measured']}. "
            "The tariff table in gemini_router.tariffs is stale; re-measure.")


def cmd_quota(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    r = _router(cfg)
    rows = r.quota_table()
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title="Gemini quota (per key×model)", title_style="bold cyan", expand=True)
    for col in ("key", "model", "RPD left", "RPM win", "TPM win", "cooldown", "next"):
        t.add_column(col)
    for row in rows:
        wait = f"{row['wait_s']:.0f}s ({row['reason']})" if row["wait_s"] > 0 else "—"
        style = "green" if row["rpd_left"] > 0 and row["wait_s"] <= 0 else (
            "yellow" if row["rpd_left"] > 0 else "red")
        rpm_w = wait if row["reason"] == "rpm" else ""
        tpm_w = wait if row["reason"] == "tpm" else ""
        t.add_row(row["key_id"], f"[{style}]{row['model']}[/{style}]",
                  f"{row['rpd_left']}/{row['rpd_limit']}",
                  f"{row['rpm_used']}/{row['rpm_limit']} {rpm_w}".strip(),
                  f"{row['tpm_used']}/{row['tpm_limit']} {tpm_w}".strip(),
                  iso(row["cooldown_until"])[:19] if row["cooldown_until"] else "—",
                  "★" if row["recommended"] else "")
    console.print(t)
    if line := _drift_line(r.ledger):
        console.print(f"[yellow]tariff drift: {line}[/yellow]")
    return 0


def cmd_models(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    async def _run() -> dict:
        r = _router(cfg)
        try:
            if args.offline:
                return {}
            # per-key: the output has a per_key section, which a shared probe would
            # fill with one answer copied N times.
            return await r.availability_per_key()
        finally:
            await r.aclose()
    avail = asyncio.run(_run())
    union: set[str] = set()
    for s in avail.values():
        if s:
            union |= s
    out = {"chain": cfg.chain, "overflow_tiers": cfg.overflow_tiers,
           "gemma_pick": pick_gemma(union) if union else None,
           "per_key": {k: (sorted(v) if v else "listModels unavailable → trust config")
                       for k, v in avail.items()},
           "missing_from_live": sorted(m for m in cfg.chain if union and m not in union)}
    blob = json.dumps(out, default=str)
    if args.json:
        print(blob)
    else:
        console.print_json(blob)
    return 0


def cmd_calls(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    rows = Ledger(cfg.state_path).calls_tail(
        args.n, model=args.model, status=args.status, source=args.source,
        request_id=getattr(args, "request_id", None),
        initiator=getattr(args, "initiator", None),
        job=getattr(args, "job", None), trace_id=getattr(args, "trace", None),
        step=getattr(args, "step", None))
    wide = getattr(args, "wide", False)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title=f"Last {len(rows)} calls · gemini-router {__version__}", expand=True)
    cols = ["ts (UTC)", "job", "step", "src", "initiator", "v", "kind", "key",
            "model", "status", "http", "in", "out", "ms", "tier"]
    if wide:
        cols += ["rid", "trace", "hop", "req.model", "signal", "quota_id", "comment"]
    for col in cols:
        t.add_column(col, overflow="fold")
    for r in reversed(rows):
        style = {"ok": "green", "quota": "red", "http5xx": "yellow",
                 "network": "yellow", "blocked": "magenta"}.get(r["status"], "dim")
        row = [r["ts_iso"][:19], (r.get("job") or "-")[:22], r.get("step") or "-",
               r["source"], r.get("initiator") or "-",
               r.get("version") or "-", r["kind"], r["key_id"], r["model"],
               f"[{style}]{r['status']}[/{style}]", str(r["http_status"] or ""),
               str(r["tokens_in"] or ""), str(r["tokens_out"] or ""),
               str(r["latency_ms"] or ""), str(r["overflow_tier"])]
        if wide:
            row += [r.get("request_id") or "-", (r.get("trace_id") or "-")[:20],
                    str(r.get("hop") or 0), r.get("requested_model") or "-",
                    r.get("signal_kind") or "-", (r.get("quota_id") or "-")[:28],
                    (r.get("comment") or "-")[:60]]
        t.add_row(*row)
    console.print(t)
    return 0


def cmd_note(args, cfg: Config | None = None) -> int:
    """Write an arbitrary comment into the shared audit trail. Nothing else happens."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    led = Ledger(cfg.state_path)
    initiator = getattr(args, "initiator", "cli") or "cli"
    job = getattr(args, "job", None)
    trace = getattr(args, "trace", None)
    step = getattr(args, "step", None)
    note_id = led.add_note(args.comment, initiator=initiator,
                           version=__version__, source="cli", job=job,
                           trace_id=trace or job, step=step or None)
    line = audit_line("note", v=__version__, initiator=initiator, job=job or "-",
                      trace=trace or job or "-", step=step or "-", note_id=note_id,
                      comment=args.comment)
    _emit_audit(line, args)
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "note_id": note_id,
                          "version": __version__, "job": job}))
    else:
        console.print(f"[green]note #{note_id} recorded[/green] (no provider call, no quota)")
    return 0


def cmd_notes(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    rows = Ledger(cfg.state_path).notes_tail(
        args.n, job=getattr(args, "job", None))
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title=f"Last {len(rows)} notes · gemini-router {__version__}", expand=True)
    for col in ("ts (UTC)", "event", "job", "step", "v", "initiator", "comment"):
        t.add_column(col, overflow="fold")
    for r in reversed(rows):
        t.add_row(r["ts_iso"][:19], r.get("event") or "note",
                  (r.get("job") or "-")[:22], r.get("step") or "-",
                  r["version"] or "-", r["initiator"] or "-", r["comment"])
    console.print(t)
    return 0


def cmd_jobs(args, cfg: Config | None = None) -> int:
    """List target activities with their counters."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    rows = Ledger(cfg.state_path).jobs_tail(getattr(args, "n", 20))
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title=f"Last {len(rows)} jobs · gemini-router {__version__}", expand=True)
    for col in ("started (UTC)", "job", "initiator", "status", "calls", "ok", "refusals",
                "429", "models"):
        t.add_column(col, overflow="fold")
    for r in reversed(rows):
        t.add_row(iso(r["first_ts"])[:19], r["job"], r.get("initiator") or "-",
                  r["status"], str(r["calls"]), str(r["ok"]), str(r["refusals"]),
                  str(r.get("quota_429") or 0), ", ".join(r["models"])[:40])
    console.print(t)
    return 0


def cmd_job(args, cfg: Config | None = None) -> int:
    """Show one activity: summary, lifecycle events and every call."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    led = Ledger(cfg.state_path)
    summary = led.job_summary(args.job)
    if not summary:
        console.print(f"[red]job {args.job!r} not found[/red]")
        return 1
    calls = led.job_calls(args.job, getattr(args, "n", 200))
    events = led.notes_tail(getattr(args, "n", 200), job=args.job)
    if getattr(args, "json", False):
        print(json.dumps({"version": __version__, "summary": summary,
                          "events": events, "calls": calls}, indent=1, default=str))
        return 0
    console.print(f"[bold cyan]job {args.job}[/bold cyan] · v{__version__} · "
                  f"{summary['status']} · {summary['calls']} calls · "
                  f"{summary['ok']} ok · {summary['refusals']} refusals · "
                  f"{summary.get('quota_429', 0)} 429")
    console.print(f"models: {', '.join(summary['models'])} · keys: "
                  f"{', '.join(summary['keys'])} · steps: "
                  f"{', '.join(summary['steps']) or '-'} · traces: "
                  f"{', '.join(summary['traces'])[:60] or '-'}")
    t = Table(title=f"job {args.job} — timeline", expand=True)
    for col in ("ts (UTC)", "event", "status/http", "step", "model", "key", "hop", "rid"):
        t.add_column(col, overflow="fold")
    timeline = [("event", e["ts_iso"], e) for e in events] + \
               [("call", c["ts_iso"], c) for c in calls]
    for kind, _, rec in sorted(timeline, key=lambda x: x[1], reverse=True):
        if kind == "event":
            t.add_row(rec["ts_iso"][:19], rec.get("event") or "note", "-", "-", "-",
                      "-", "-", rec.get("comment") or "-")
        else:
            t.add_row(rec["ts_iso"][:19], f"[{rec['status']}]", str(rec["http_status"] or ""),
                      rec.get("step") or "-", rec["model"], rec["key_id"],
                      str(rec.get("hop") or 0), rec.get("request_id") or "-")
    console.print(t)
    return 0


def cmd_job_action(args, cfg: Config | None = None) -> int:
    """Start or finish an activity (local bookkeeping, no provider call)."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    led = Ledger(cfg.state_path)
    action = "start" if args.cmd == "job-start" else "finish"
    if action == "start":
        out = led.job_start(args.job, initiator=getattr(args, "initiator", "") or "",
                            version=__version__, comment=getattr(args, "comment", "") or "",
                            trace_id=getattr(args, "trace", None),
                            step=getattr(args, "step", None))
    else:
        out = led.job_finish(args.job, status=getattr(args, "status", "done"),
                             initiator=getattr(args, "initiator", "") or "",
                             version=__version__, comment=getattr(args, "comment", "") or "",
                             step=getattr(args, "step", None))
    status = out["status"] if action == "start" else getattr(args, "status", "done")
    _emit_audit(audit_line("job", v=__version__, action=action, job=args.job,
                           status=status, comment=getattr(args, "comment", "") or "-"), args)
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "job": args.job, "action": action,
                          "summary": out}, default=str))
    else:
        console.print(f"[green]{action}[/green] · job {args.job} · {out['calls']} calls")
    return 0


def cmd_prune(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    res = Ledger(cfg.state_path).prune(
        audit_keep_days=args.audit_days, cache_ttl_s=cfg.cache_ttl_h * 3600, dry=args.dry)
    console.print(("DRY " if args.dry else "") + json.dumps(res))
    return 0


def cmd_doctor(args, cfg: Config | None = None) -> int:
    """Exit code names the failure, so a script can act on it without parsing prose:
    0 ok · 3 no keys · 4 state file unusable · 5 no reachable chain."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    console.print(f"[bold]gemini-router {__version__} doctor[/bold]")
    if not Path(cfg.state_path).exists():
        console.print(f"  state: {cfg.state_path} (not created yet — no calls recorded)")
    else:
        console.print(f"  state: {cfg.state_path}")
        try:
            led = Ledger(cfg.state_path)
            console.print(f"  state stats: {led.stats()}")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]state unusable: {e}[/red]")
            return 4
        if line := _drift_line(led):
            console.print(f"  [yellow]tariff drift: {line}[/yellow]")
    if cfg.api_keys:
        from .config import key_id
        console.print(f"  keys: {len(cfg.api_keys)} → "
                      + ", ".join(key_id(k) for k in cfg.api_keys))
    else:
        console.print("  [red]no API keys configured "
                      "(GEMINI_API_KEYS / GEMINI_API_KEY_1..10)[/red]")
        console.print("[red]FAILED[/red] — nothing to route with")
        return 3
    tz_now = pt_midnight_ts(cfg.quota_day_tz)
    console.print(f"  quota day ({cfg.quota_day_tz}): resets in "
                  f"{(pt_midnight_ts(cfg.quota_day_tz, days_ahead=1) - tz_now) / 3600:.1f} h "
                  f"(midnight was {iso(tz_now)[:19]} UTC)")
    console.print(f"  chain: {' → '.join(cfg.chain)}")
    console.print(f"  limits/key×model: {cfg.limits_per_key_model.model_dump()} · "
                  f"overflow: {cfg.overflow_limits.model_dump()} · "
                  f"wait: {cfg.wait.model_dump()}")
    if args.offline:
        console.print("[green]OK[/green] (offline — connectivity not checked)")
        return 0

    live = 0
    chain_live = False

    async def _probe() -> None:
        nonlocal live, chain_live
        r = _router(cfg)
        try:
            # per-key: doctor reports each key separately, and one shared probe would
            # print the same answer N times as though each key had been checked.
            for kid, models in (await r.availability_per_key()).items():
                if models is None:
                    console.print(f"  [yellow]key {kid}: listModels failed "
                                  "(offline, or the key is refused)[/yellow]")
                    continue
                live += 1
                have = [m for m in cfg.chain if m in models]
                chain_live = chain_live or bool(have)
                console.print(f"  key {kid}: {len(models)} models live; chain available: "
                              + (", ".join(have) or "[red]NONE[/red]"))
        finally:
            await r.aclose()

    asyncio.run(_probe())
    if not chain_live:
        console.print("[red]FAILED[/red] — no configured chain model is reachable on any "
                      f"key ({live} key(s) answered); check the keys, or `chain` against "
                      "the live model list")
        return 5
    console.print("[green]OK[/green]")
    return 0


def _format_eta(seconds: float) -> str:
    total = max(0, int(float(seconds) + 0.5))
    if total >= 3600:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}:{total % 60:02d}"
    return f"{total}s"


def cmd_tts(args, cfg: Config | None = None) -> int:
    """Read TXT/Markdown, synthesize in quota-sized chunks, and write one MP3."""
    cfg = cfg if cfg is not None else _load_cfg(args)
    progress = Progress(
        TextColumn("{task.description}", markup=False),
        BarColumn(bar_width=24),
        TaskProgressColumn(),
        TextColumn("{task.fields[eta]}", style="dim", justify="right"),
        console=console,
        transient=False,
        refresh_per_second=8,
        disable=bool(args.json) or not console.is_terminal,
    )
    with progress:
        task = progress.add_task("Preparing audio", total=100, eta="")
        completed = 0.0
        eta_text = ""

        def on_event(event: RouterEvent) -> None:
            nonlocal completed, eta_text
            if event.pct is not None:
                completed = max(completed, min(100.0, float(event.pct)))
            eta_s = None
            if event.stage == "file_progress":
                description = str(event.detail.get("label") or "Preparing audio")
                eta_s = event.detail.get("eta_s")
                eta_text = f"· ~{_format_eta(eta_s)}" if eta_s is not None else ""
            elif event.stage == "waiting":
                description = "Waiting"
                eta_s = event.wait_s
                eta_text = f"· ~{_format_eta(eta_s)}" if eta_s is not None else ""
            elif event.stage == "dispatch":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"TTS · {chunk}/{chunks}" if chunk and chunks else "TTS"
                eta_text = ""
            elif event.stage == "retry":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"Retry · {chunk}/{chunks}" if chunk and chunks else "Retry"
                eta_text = ""
            elif event.stage == "switch":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"Switch · {chunk}/{chunks}" if chunk and chunks else "Switch"
                eta_text = ""
            else:
                return
            progress.update(
                task, completed=completed, description=description, eta=eta_text
            )
            progress.refresh()

        async def _run():
            r = _router(cfg)
            try:
                return await r.synthesize_file(
                    args.file, output_path=args.output, voice=args.voice, style=args.style,
                    bitrate_kbps=args.bitrate, wait=args.wait, source="cli",
                    initiator="gemini-router-cli", request_id=args.request_id,
                    on_event=None if args.json else on_event,
                )
            finally:
                await r.aclose()

        result = asyncio.run(_run())
    payload = {"path": str(result.path), "model": result.model,
               "chunks": result.chunks, "tokens_in": result.tokens_in,
               "key_ids": result.key_ids, "latency_s": round(result.latency_s, 2)}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        console.print(f"[green]Wrote {result.path}[/green] · {result.chunks} chunk(s) · "
                      f"{result.tokens_in} input tokens · {result.latency_s:.1f}s")
    return 0


def cmd_proxy(args) -> int:
    """Run the native-Gemini pass-through (`GOOGLE_GEMINI_BASE_URL` for Gen AI SDKs).

    Fails fast and loudly when the client token is missing: an open port in front of the
    key pool with no credential is not a degraded service, it is a credential.
    """
    from .proxy import serve

    over: dict = {}
    if getattr(args, "bind", None):
        over["proxy"] = {**_proxy_cfg(args), "bind": args.bind}
    if getattr(args, "port", None):
        over["proxy"] = {**_proxy_cfg(args), "port": args.port}
    cfg = Config.load(getattr(args, "config", None),
                      state_path=getattr(args, "state", None), **over)
    if not cfg.api_keys:
        console.print("[red]error:[/red] no API keys: set GEMINI_API_KEYS or "
                      "GEMINI_API_KEY_1..10")
        return 3
    import os

    if not os.environ.get(cfg.proxy.token_env):
        console.print(f"[red]error:[/red] {cfg.proxy.token_env} is not set — mint one with "
                      f"`openssl rand -hex 32` and put it in the unit's EnvironmentFile")
        return 2
    serve(cfg)
    return 0


def _proxy_cfg(args) -> dict:
    """The proxy sub-config merged from whatever the flags already set."""
    cfg = Config.load(getattr(args, "config", None),
                      state_path=getattr(args, "state", None))
    return cfg.proxy.model_dump()


_COMMANDS = {"quota": cmd_quota, "models": cmd_models, "calls": cmd_calls,
             "prune": cmd_prune, "doctor": cmd_doctor, "proxy": cmd_proxy,
             "note": cmd_note, "notes": cmd_notes, "jobs": cmd_jobs,
             "job": cmd_job, "job-start": cmd_job_action, "job-finish": cmd_job_action,
             "tts": cmd_tts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gemini-router",
                                 description="Quota-aware Gemini Flash router CLI")
    ap.add_argument("--config", help="YAML config path (or GEMINI_ROUTER_CONFIG)")
    ap.add_argument("--state", help="override state db path (or GEMINI_ROUTER_STATE)")
    ap.add_argument("--version", action="version", version=f"gemini-router {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("quota", help="live budget table (which model can take a request)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("models", help="resolved chain vs live listModels")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("calls", help="audit log tail")
    p.add_argument("-n", type=int, default=50)
    for flag in ("--model", "--status", "--source", "--request-id", "--initiator",
                 "--job", "--trace", "--step"):
        p.add_argument(flag)
    p.add_argument("--json", action="store_true")
    p.add_argument("--wide", action="store_true",
                   help="also show request id, provider signal, quota id and comment")
    p = sub.add_parser("note", help="record a free-form comment (no provider call)")
    p.add_argument("comment", help='e.g. "начинаем тестирование новой версии"')
    p.add_argument("--initiator", default="cli", help='who wrote it, e.g. "ops"')
    p.add_argument("--job", help="attach this note to a job/trace")
    p.add_argument("--trace")
    p.add_argument("--step")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("notes", help="show recent annotations and job events")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--job")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("jobs", help="list target activities (jobs) with counters")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("job", help="show one job: summary, events, all calls")
    p.add_argument("job")
    p.add_argument("-n", type=int, default=200)
    p.add_argument("--json", action="store_true")
    for name in ("job-start", "job-finish"):
        p = sub.add_parser(name, help=f"{name.split('-')[1]} a target activity")
        p.add_argument("job")
        p.add_argument("--initiator", default="")
        p.add_argument("--comment", default="")
        p.add_argument("--trace")
        p.add_argument("--step")
        p.add_argument("--status", default="done")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("prune", help="apply retention (audit days, cache TTL)")
    p.add_argument("--audit-days", type=float, default=30)
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("doctor", help="config/state/connectivity check")
    p.add_argument("--offline", action="store_true")
    p = sub.add_parser("tts", help="synthesize a TXT/Markdown file into one MP3")
    p.add_argument("file", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--voice")
    p.add_argument("--style")
    p.add_argument("--bitrate", type=int, default=128, help="MP3 bitrate in kbps")
    p.add_argument("--wait", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--request-id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("proxy", help="native Gemini API pass-through (File Search MCP)")
    p.add_argument("--bind", help="listen address (default from config: 127.0.0.1)")
    p.add_argument("--port", type=int, help="listen port (default from config: 8787)")

    args = ap.parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except RouterError as e:
        console.print(f"[red]error:[/red] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
