"""CLI smoke: quota/calls/prune/doctor work against a state file without network
(FR-C12, FR-E7). Run with GEMINI_API_KEYS set via monkeypatch."""
from __future__ import annotations

import json

import pytest

from gemini_router.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.setenv("GEMINI_ROUTER_STATE", str(tmp_path / "state.db"))
    monkeypatch.delenv("GEMINI_ROUTER_CONFIG", raising=False)
    return tmp_path


def test_quota_json(env, capsys):
    assert main(["quota", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) >= 4                                      # chain models × 2 keys
    assert rows[0]["rpd_left"] == rows[0]["rpd_limit"]
    assert sum(1 for r in rows if r["recommended"]) == 1


def test_doctor_offline(env, capsys):
    assert main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "keys: 2" in out and "OK" in out


def test_calls_and_prune(env, capsys):
    assert main(["calls", "-n", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["prune", "--dry"]) == 0
    assert "DRY" in capsys.readouterr().out


def test_models_offline(env, capsys):
    assert main(["models", "--offline", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["chain"] and out["gemma_pick"] is None


def test_no_keys_is_clear_error(tmp_path, monkeypatch, capsys):
    for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for i in range(1, 11):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    rc = main(["--state", str(tmp_path / "s.db"), "quota"])
    out = capsys.readouterr().out
    assert rc == 2 and "no api keys" in out.lower()
