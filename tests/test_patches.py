"""Patch-system sanity tests (no root / no live cPanel required)."""
from __future__ import annotations

from cpanel_doctor.core import Patch, PatchState, Runner, load_patches, patches_by_id
from cpanel_doctor.patches.pg_cpses import PgCpsesPatch


def test_patches_discovered():
    patches = load_patches()
    assert patches, "no patches discovered"
    assert all(isinstance(p, Patch) for p in patches)
    assert "pg-cpses" in patches_by_id()


def test_pg_cpses_components_well_formed():
    patch = PgCpsesPatch()
    comps = patch.components()
    keys = [c.key for c in comps]
    assert keys == ["validator", "pam", "loopback", "phppgadmin"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_state_is_not_applicable_off_cpanel(tmp_path, monkeypatch):
    # On a machine without phpPgAdmin/psql the patch must report NOT_APPLICABLE,
    # never crash and never claim to be applied.
    patch = PgCpsesPatch()
    runner = Runner()
    applicable, _ = patch.applicable(runner)
    if not applicable:
        assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_dry_run_changes_nothing(tmp_path):
    # Apply against a temp validator path in dry-run: file must NOT be created.
    target = tmp_path / "validator.sh"
    runner = Runner(dry_run=True)
    runner.write(str(target), "hello", mode=0o755)
    assert not target.exists()
    assert runner.actions and runner.actions[-1].kind == "write"
