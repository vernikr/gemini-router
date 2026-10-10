"""Terminal UX (FR-C12): quota | models | calls | prune | doctor. Works over SSH
against the shared state file without touching the daemon (FR-E7)."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Config
from .errors import RouterError
from .ledger import Ledger, iso, pt_midnight_ts
from .routing import Router, pick_gemma

console = Console()


def _load_cfg(args) -> Config:
    over = {"state_path": args.state} if getattr(args, "state", None) else {}
    return Config.load(getattr(args, "config", None), **over)


def _router(cfg: Config) -> Router:
    return Router(cfg, ledger=Ledger(cfg.state_path))


def cmd_quota(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    rows = _router(cfg).quota_table()
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title="Gemini quota (per key×model)", title_style="bold cyan", expand=True)
    for col in ("key", "model", "RPD left", "RPM win", "TPM win", "cooldown", "next"):
        t.add_column(col)
    for r in rows:
        wait = f"{r['wait_s']:.0f}s ({r['reason']})" if r["wait_s"] > 0 else "—"
        style = "green" if r["rpd_left"] > 0 and r["wait_s"] <= 0 else (
            "yellow" if r["rpd_left"] > 0 else "red")
        rpm_w = wait if r["reason"] == "rpm" else ""
        tpm_w = wait if r["reason"] == "tpm" else ""
        t.add_row(r["key_id"], f"[{style}]{r['model']}[/{style}]",
                  f"{r['rpd_left']}/{r['rpd_limit']}",
                  f"{r['rpm_used']}/{r['rpm_limit']} {rpm_w}".strip(),
                  f"{r['tpm_used']}/{r['tpm_limit']} {tpm_w}".strip(),
                  iso(r["cooldown_until"])[:19] if r["cooldown_until"] else "—",
                  "★" if r["recommended"] else "")
    console.print(t)
    return 0


def cmd_models(args, cfg: Config | None = None) -> int:
    cfg = cfg if cfg is not None else _load_cfg(args)
    async def _run() -> dict:
        r = _router(cfg)
        try:
            return await r.availability() if not args.offline else {}
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
        request_id=args.request_id)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    t = Table(title=f"Last {len(rows)} calls", expand=True)
    for col in ("ts (UTC)", "src", "kind", "key", "model", "status", "http",
                "in", "out", "ms", "tier"):
        t.add_column(col, overflow="fold")
    for r in reversed(rows):
        style = {"ok": "green", "quota": "red", "http5xx": "yellow",
                 "network": "yellow", "blocked": "magenta"}.get(r["status"], "dim")
        t.add_row(r["ts_iso"][:19], r["source"], r["kind"], r["key_id"], r["model"],
                  f"[{style}]{r['status']}[/{style}]", str(r["http_status"] or ""),
                  str(r["tokens_in"] or ""), str(r["tokens_out"] or ""),
                  str(r["latency_ms"] or ""), str(r["overflow_tier"]))
    console.print(t)
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
            console.print(f"  state stats: {Ledger(cfg.state_path).stats()}")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]state unusable: {e}[/red]")
            return 4
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
            for kid, models in (await r.availability()).items():
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


_COMMANDS = {"quota": cmd_quota, "models": cmd_models, "calls": cmd_calls,
             "prune": cmd_prune, "doctor": cmd_doctor}


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
    for flag in ("--model", "--status", "--source", "--request-id"):
        p.add_argument(flag)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("prune", help="apply retention (audit days, cache TTL)")
    p.add_argument("--audit-days", type=float, default=30)
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("doctor", help="config/state/connectivity check")
    p.add_argument("--offline", action="store_true")

    args = ap.parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except RouterError as e:
        console.print(f"[red]error:[/red] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
