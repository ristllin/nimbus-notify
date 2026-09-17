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

def _toml():
    if sys.version_info < (3, 11):
        import tomli as tomllib
    else:
        import tomllib
    return tomllib


def test_vibe_matches_reference():
    tomllib = _toml()
    ref_text = (REPO / "hooks" / "vibe" / "hooks.toml").read_text()
    ref = tomllib.loads(ref_text)["hooks"]
    embedded = tomllib.loads(install.VIBE_HOOKS_TOML)["hooks"]
    assert embedded == ref


def test_default_template_uses_new_names_and_is_valid():
    tomllib = _toml()
    hooks = tomllib.loads(install.VIBE_HOOKS_TOML)["hooks"]
    types = {h["type"] for h in hooks}
    assert types == {"pre_tool", "post_tool", "post_agent"}
    # post_agent must carry NO `match` (Vibe's model validator rejects it there)
    post_agent = next(h for h in hooks if h["type"] == "post_agent")
    assert "match" not in post_agent
    assert all(h["command"].startswith("led-report vibe ") for h in hooks)


def test_legacy_template_uses_old_names_and_is_valid():
    tomllib = _toml()
    hooks = tomllib.loads(install.VIBE_HOOKS_TOML_LEGACY)["hooks"]
    types = {h["type"] for h in hooks}
    assert types == {"before_tool", "after_tool", "post_agent_turn"}


def test_vibe_default_writes_new_names(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False)
    text = (tmp_path / ".vibe" / "hooks.toml").read_text()
    assert install._VIBE_SENTINEL in text
    assert 'type    = "pre_tool"' in text
    assert "before_tool" not in text
    # default path does NOT write the experimental flag (removed in 2.21)
    cfg = tmp_path / ".vibe" / "config.toml"
    assert not cfg.exists() or install._VIBE_FLAG not in cfg.read_text()


def test_vibe_legacy_writes_old_names_and_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False, legacy=True)
    text = (tmp_path / ".vibe" / "hooks.toml").read_text()
    assert 'type    = "before_tool"' in text
    assert "pre_tool" not in text
    assert install._VIBE_FLAG in (tmp_path / ".vibe" / "config.toml").read_text()


def test_vibe_upgrade_rewrites_old_to_new(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # a machine with the pre-2.21 file already installed (the stale-ns-* case)
    install.install_vibe(dry_run=False, legacy=True)
    hooks_file = tmp_path / ".vibe" / "hooks.toml"
    assert "before_tool" in hooks_file.read_text()
    # re-run in the DEFAULT (new) mode -> upgrade, not skip-on-sentinel
    install.install_vibe(dry_run=False)
    text = hooks_file.read_text()
    assert 'type    = "pre_tool"' in text
    assert "before_tool" not in text and "post_agent_turn" not in text
    # exactly our three current blocks (no duplicates / leftovers)
    tomllib = _toml()
    hooks = tomllib.loads(text)["hooks"]
    assert {h["type"] for h in hooks} == {"pre_tool", "post_tool", "post_agent"}
    # and the stale experimental flag is cleaned out of config.toml
    cfg = tmp_path / ".vibe" / "config.toml"
    assert not cfg.exists() or install._VIBE_FLAG not in cfg.read_text()


def test_vibe_upgrade_preserves_unrelated_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hooks_file = tmp_path / ".vibe" / "hooks.toml"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        '[[hooks]]\nname = "user-guard"\ntype = "pre_tool"\nmatch = "*"\n'
        'command = "my-guard.sh"\ntimeout = 5.0\n')
    install.install_vibe(dry_run=False)
    tomllib = _toml()
    hooks = tomllib.loads(hooks_file.read_text())["hooks"]
    cmds = {h["command"] for h in hooks}
    assert "my-guard.sh" in cmds                          # user's hook survived
    assert any(c.startswith("led-report vibe") for c in cmds)  # ours added


def test_vibe_hooks_written(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False)
    hooks_file = tmp_path / ".vibe" / "hooks.toml"
    assert hooks_file.exists()
    assert install._VIBE_SENTINEL in hooks_file.read_text()


def test_vibe_idempotent_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False)
    first = (tmp_path / ".vibe" / "hooks.toml").read_text()
    install.install_vibe(dry_run=False)   # re-run must be byte-identical
    assert (tmp_path / ".vibe" / "hooks.toml").read_text() == first


def test_vibe_idempotent_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=False, legacy=True)
    first_hooks = (tmp_path / ".vibe" / "hooks.toml").read_text()
    first_cfg = (tmp_path / ".vibe" / "config.toml").read_text()
    install.install_vibe(dry_run=False, legacy=True)
    assert (tmp_path / ".vibe" / "hooks.toml").read_text() == first_hooks
    assert (tmp_path / ".vibe" / "config.toml").read_text() == first_cfg


def test_vibe_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_vibe(dry_run=True)
    assert not (tmp_path / ".vibe" / "hooks.toml").exists()
    assert not (tmp_path / ".vibe" / "config.toml").exists()


def test_vibe_legacy_preserves_existing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".vibe" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('personality = "friendly"\n[features]\nfoo = true\n')
    install.install_vibe(dry_run=False, legacy=True)
    text = cfg.read_text()
    assert 'personality = "friendly"' in text
    assert "[features]" in text
    assert install._VIBE_FLAG in text
    assert (cfg.with_suffix(".toml.bak")).exists()


def test_vibe_default_leaves_flagless_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".vibe" / "config.toml"
    cfg.parent.mkdir(parents=True)
    original = 'personality = "friendly"\n[features]\nfoo = true\n'
    cfg.write_text(original)
    install.install_vibe(dry_run=False)
    assert cfg.read_text() == original          # no flag added, nothing rewritten


def test_insert_vibe_flag_no_sections(tmp_path):
    result = install._insert_vibe_flag('key = "value"\n')
    assert result.endswith(install._VIBE_FLAG + "\n")
    assert 'key = "value"' in result


def test_insert_vibe_flag_idempotent():
    text = install._VIBE_FLAG + "\n[section]\nfoo = true\n"
    assert install._insert_vibe_flag(text) == text


def test_remove_vibe_flag():
    text = install._VIBE_FLAG + '\n[section]\nfoo = true\n'
    assert install._VIBE_FLAG not in install._remove_vibe_flag(text)
    assert "[section]" in install._remove_vibe_flag(text)


# ---------------------------------------------------------------------------
# doctor: Vibe version <-> hook-name drift (the CUM-414 root cause)
# ---------------------------------------------------------------------------

def _write_vibe_hooks_file(home, template):
    p = home / ".vibe" / "hooks.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(template)
    return p


def test_doctor_flags_old_names_on_new_vibe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(install, "_detect_vibe_version", lambda: (2, 25, 4))
    _write_vibe_hooks_file(tmp_path, install.VIBE_HOOKS_TOML_LEGACY)
    rc = install.doctor()
    out = capsys.readouterr().out
    assert "pre-2.21 names" in out
    assert "install-hooks --harness vibe" in out
    assert rc == 1                                  # a red doctor


def test_doctor_flags_new_names_on_old_vibe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(install, "_detect_vibe_version", lambda: (2, 19, 0))
    _write_vibe_hooks_file(tmp_path, install.VIBE_HOOKS_TOML)
    rc = install.doctor()
    out = capsys.readouterr().out
    assert "--vibe-legacy" in out
    assert rc == 1


def test_doctor_ok_new_names_new_vibe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(install, "_detect_vibe_version", lambda: (2, 25, 4))
    _write_vibe_hooks_file(tmp_path, install.VIBE_HOOKS_TOML)
    install.doctor()
    out = capsys.readouterr().out
    assert "v2.21+" in out
    assert "pre-2.21 names" not in out


def test_detect_vibe_version_lenient(monkeypatch):
    import subprocess

    class _R:
        def __init__(self, stdout="", stderr=""):
            self.stdout, self.stderr = stdout, stderr

    for text, expected in [
        ("vibe 2.25.4\n", (2, 25, 4)),
        ("mistral-vibe 2.19.0", (2, 19, 0)),
        ("__main__.py 2.21.0 (build abc)", (2, 21, 0)),
    ]:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(stdout=text))
        assert install._detect_vibe_version() == expected

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert install._detect_vibe_version() is None
