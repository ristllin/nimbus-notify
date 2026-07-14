"""Tests for `nimbus-notify install-hooks` — the onboarding installer.

The installer embeds the canonical hook wiring (so a pip user needs no files on
disk); these tests assert it (a) reproduces the reference hooks/*.json exactly (no
drift vs the plugin path), (b) preserves the user's pre-existing hooks, and (c) is
idempotent.
"""
import json
import sys
from pathlib import Path

from notify.cli import install

REPO = Path(__file__).resolve().parents[1]


def test_claude_matches_reference():
    ref = json.loads((REPO / "hooks" / "claude" / "settings.json").read_text())["hooks"]
    assert install.build_claude_hooks() == ref


def test_codex_matches_reference():
    ref = json.loads((REPO / "hooks" / "codex" / "hooks.json").read_text())["hooks"]
    assert install.build_codex_hooks()["hooks"] == ref


def test_merge_preserves_unrelated_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "my-existing.sh"}]}]}}))

    install.install_claude(dry_run=False)

    d = json.loads(settings.read_text())
    start = d["hooks"]["SessionStart"]
    cmds = [h["command"] for g in start for h in g["hooks"]]
    assert "my-existing.sh" in cmds                       # unrelated hook survived
    assert "led-report claude start --pid $PPID" in cmds  # ours appended
    assert set(d["hooks"]) >= {"SessionStart", "Stop", "SessionEnd", "Notification"}
    assert (tmp_path / ".claude" / "settings.json.bak").exists()  # backed up


def test_idempotent_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_claude(dry_run=False)
    first = (tmp_path / ".claude" / "settings.json").read_text()
    install.install_claude(dry_run=False)   # re-run
    second = (tmp_path / ".claude" / "settings.json").read_text()
    assert first == second                  # no duplication on re-run


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_claude(dry_run=True)
    assert not (tmp_path / ".claude" / "settings.json").exists()


# ---------------------------------------------------------------------------
# Vibe
# ---------------------------------------------------------------------------

def test_vibe_matches_reference():
    if sys.version_info < (3, 11):
        import tomli as tomllib
    else:
        import tomllib
    ref_text = (REPO / "hooks" / "vibe" / "hooks.toml").read_text()
    ref = tomllib.loads(ref_text)["hooks"]
    embedded = tomllib.loads(install.VIBE_HOOKS_TOML)["hooks"]
    assert embedded == ref


def test_vibe_hooks_written(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False)
    hooks_file = tmp_path / ".vibe" / "hooks.toml"
    assert hooks_file.exists()
    assert install._VIBE_SENTINEL in hooks_file.read_text()


def test_vibe_config_flag_inserted(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".vibe" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[model]\nname = "mistral-large"\n')
    install.install_vibe(dry_run=False)
    text = cfg.read_text()
    assert install._VIBE_FLAG in text
    # flag must appear before the first [section] so Vibe reads it
    flag_pos = text.index(install._VIBE_FLAG)
    section_pos = text.index("[")
    assert flag_pos < section_pos


def test_vibe_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False)
    first_hooks = (tmp_path / ".vibe" / "hooks.toml").read_text()
    first_cfg = (tmp_path / ".vibe" / "config.toml").read_text()
    install.install_vibe(dry_run=False)
    assert (tmp_path / ".vibe" / "hooks.toml").read_text() == first_hooks
    assert (tmp_path / ".vibe" / "config.toml").read_text() == first_cfg


def test_vibe_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=True)
    assert not (tmp_path / ".vibe" / "hooks.toml").exists()
    assert not (tmp_path / ".vibe" / "config.toml").exists()


def test_vibe_preserves_existing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".vibe" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('personality = "friendly"\n[features]\nfoo = true\n')
    install.install_vibe(dry_run=False)
    text = cfg.read_text()
    assert 'personality = "friendly"' in text
    assert "[features]" in text
    assert install._VIBE_FLAG in text
    assert (cfg.with_suffix(".toml.bak")).exists()


def test_insert_vibe_flag_no_sections(tmp_path):
    result = install._insert_vibe_flag('key = "value"\n')
    assert result.endswith(install._VIBE_FLAG + "\n")
    assert 'key = "value"' in result


def test_insert_vibe_flag_idempotent():
    text = install._VIBE_FLAG + "\n[section]\nfoo = true\n"
    assert install._insert_vibe_flag(text) == text
