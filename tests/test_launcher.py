from __future__ import annotations

import subprocess

from gemini_router import launcher


def test_version_key_accepts_release_versions():
    assert launcher._version_key("0.14.0") == (0, 14, 0)
    assert launcher._version_key("0.14.0rc1") == (0, 14, 0)
    assert launcher._version_key("invalid") is None


def test_no_update_flag_is_removed_and_skips_registry(monkeypatch):
    seen = {}
    monkeypatch.setattr(launcher, "_source_checkout", lambda: False)
    monkeypatch.setattr(launcher, "_latest_version", lambda: (_ for _ in ()).throw(
        AssertionError("registry must not be queried")))
    monkeypatch.setattr(launcher, "_run_current", lambda args: seen.update(args=args) or 0)

    assert launcher.main(["quota", "--no-update"]) == 0
    assert seen["args"] == ["quota"]


def test_published_update_is_preflighted_then_run(monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("GEMINI_ROUTER_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(launcher, "_source_checkout", lambda: False)
    monkeypatch.setattr(launcher, "__version__", "0.13.0")
    monkeypatch.setattr(launcher, "_latest_version", lambda: "0.14.0")
    monkeypatch.setattr(launcher, "_uvx_command", lambda: ["uvx"])
    monkeypatch.setattr(launcher, "_run_current", lambda args: 99)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.main(["quota", "--json"]) == 0
    assert calls[0][0] == ["uvx", "--from", "gemini-router==0.14.0", "gemini-router", "--version"]
    assert calls[1][0] == [
        "uvx", "--from", "gemini-router==0.14.0", "gemini-router", "quota", "--json"
    ]
    assert calls[0][1]["env"]["GEMINI_ROUTER_AUTO_UPDATE"] == "0"
    assert "updated 0.14.0" in capsys.readouterr().err


def test_registry_or_install_failure_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_ROUTER_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(launcher, "_source_checkout", lambda: False)
    monkeypatch.setattr(launcher, "__version__", "0.13.0")
    monkeypatch.setattr(launcher, "_latest_version", lambda: "0.14.0")
    monkeypatch.setattr(launcher, "_uvx_command", lambda: ["uvx"])
    seen = {}
    monkeypatch.setattr(launcher, "_run_current", lambda args: seen.update(args=args) or 0)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 1))

    assert launcher.main(["models"]) == 0
    assert seen["args"] == ["models"]
    assert "using 0.13.0" in capsys.readouterr().err


def test_environment_can_disable_update(monkeypatch):
    monkeypatch.setenv("GEMINI_ROUTER_AUTO_UPDATE", "false")
    monkeypatch.setattr(launcher, "_source_checkout", lambda: False)
    monkeypatch.setattr(launcher, "_latest_version", lambda: (_ for _ in ()).throw(
        AssertionError("registry must not be queried")))
    monkeypatch.setattr(launcher, "_run_current", lambda args: 0)
    assert launcher.main(["quota"]) == 0
