"""Tests for `nimbus-notify install-allow-rules`: pre-approving the Claude Code
wake-up tools so an unattended loop can arm and retire its own wake-ups without a
permission prompt (the prompt would otherwise pin an amber "needs you" segment on
the ring with nobody watching).

These assert the merge (a) adds the wake-up rules into permissions.allow, (b)
preserves the user's pre-existing permissions, (c) is idempotent, (d) writes
nothing under --dry-run, and (e) refuses to touch a malformed file.
"""
import json
from pathlib import Path

from notify.cli import install

CLAUDE = lambda tmp: tmp / ".claude" / "settings.json"  # noqa: E731


def _read(tmp):
    return json.loads(CLAUDE(tmp).read_text())


def test_adds_rules_to_fresh_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_allow_rules(dry_run=False)
    allow = _read(tmp_path)["permissions"]["allow"]
    assert set(install.CLAUDE_ALLOW_RULES) <= set(allow)
    # the headline wake-up tools are covered
    assert "ScheduleWakeup" in allow
    assert "CronCreate" in allow


def test_preserves_existing_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = CLAUDE(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(./secrets/**)"]},
        "model": "sonnet",
    }))

    install.install_allow_rules(dry_run=False)

    d = _read(tmp_path)
    allow = d["permissions"]["allow"]
    assert "Bash(ls:*)" in allow                     # unrelated allow survived
    assert set(install.CLAUDE_ALLOW_RULES) <= set(allow)
    assert d["permissions"]["deny"] == ["Read(./secrets/**)"]  # deny untouched
    assert d["model"] == "sonnet"                    # unrelated top-level key kept
    assert settings.with_suffix(".json.bak").exists()  # backed up


def test_idempotent_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_allow_rules(dry_run=False)
    first = CLAUDE(tmp_path).read_text()
    install.install_allow_rules(dry_run=False)  # re-run
    second = CLAUDE(tmp_path).read_text()
    assert first == second                       # no duplication
    allow = _read(tmp_path)["permissions"]["allow"]
    # each rule appears exactly once
    for rule in install.CLAUDE_ALLOW_RULES:
        assert allow.count(rule) == 1


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_allow_rules(dry_run=True)
    assert not CLAUDE(tmp_path).exists()


def test_partial_existing_only_adds_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = CLAUDE(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["ScheduleWakeup"]}}))
    install.install_allow_rules(dry_run=False)
    allow = _read(tmp_path)["permissions"]["allow"]
    assert allow.count("ScheduleWakeup") == 1        # not duplicated
    assert "CronCreate" in allow                     # the missing ones added


def test_refuses_malformed_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = CLAUDE(tmp_path)
    settings.parent.mkdir(parents=True)
    # allow as a string, not a list -> refuse rather than clobber
    settings.write_text(json.dumps({"permissions": {"allow": "Bash"}}))
    install.install_allow_rules(dry_run=False)
    # file left exactly as it was; no crash
    assert _read(tmp_path)["permissions"]["allow"] == "Bash"


def test_refuses_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = CLAUDE(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("{not valid json")
    install.install_allow_rules(dry_run=False)
    assert settings.read_text() == "{not valid json"  # untouched


def test_allow_rules_wired_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = CLAUDE(tmp_path)
    assert install._allow_rules_wired(settings) is False   # missing file
    install.install_allow_rules(dry_run=False)
    assert install._allow_rules_wired(settings) is True     # after install
