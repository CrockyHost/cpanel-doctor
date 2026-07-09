"""Patch-system sanity tests (no root / no live cPanel required)."""
from __future__ import annotations

from cpanel_doctor.core import Patch, PatchState, Runner, load_patches, patches_by_id
from cpanel_doctor.patches import account_startdate as asd
from cpanel_doctor.patches import https_redirect_date as hrd
from cpanel_doctor.patches import pdns_upcp_removal as puc
from cpanel_doctor.patches.account_startdate import AccountStartdatePatch
from cpanel_doctor.patches.https_redirect_date import HttpsRedirectDatePatch
from cpanel_doctor.patches.pdns_upcp_removal import PdnsUpcpRemovalPatch
from cpanel_doctor.patches.pg_cpses import PgCpsesPatch


def test_patches_discovered():
    patches = load_patches()
    assert patches, "no patches discovered"
    assert all(isinstance(p, Patch) for p in patches)
    registry = patches_by_id()
    assert "pg-cpses" in registry
    assert "https-redirect-date" in registry
    assert "account-startdate" in registry
    assert "pdns-upcp-removal" in registry


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


def test_https_redirect_date_components_well_formed():
    patch = HttpsRedirectDatePatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["ssl_call"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


# A minimal stand-in for the vendor ssl_call.pm carrying every anchor the patch
# edits, so the round-trip can be tested without a live cPanel install.
_FIXTURE = """package Cpanel::Admin::Modules::Cpanel::ssl_call;

sub GET_SSL_VHOSTS {
    my ( $self, $vhost_cache ) = @_;
    my %vhost_hash;

    #Optimized
    foreach my $vh (@vhosts) {
        $vhost_hash{$vh}{ssl_valid} = _ssl_actually_valid( $TLSIndex, $alias_map{$vh} );
        foreach my $alias (@aliases) {
            $vhost_hash{$vh}{alias_ssl_valid}{$alias} = _ssl_actually_valid( $TLSIndex, $alias_map{$alias} );
        }
    }
    return %vhost_hash;
}

sub _ssl_actually_valid {
    my ( $TLSIndex, $dom_obj ) = @_;
    my $time = time();
    return 0 if $time < $not_before;
    return 0 if $time > $not_after;
    return 1;
}

1;
"""


def test_https_redirect_date_roundtrip(tmp_path, monkeypatch):
    # apply() against a pristine file must add _real_now and switch the validity
    # check to it; remove() must restore the original byte-for-byte.
    target = tmp_path / "ssl_call.pm"
    target.write_text(_FIXTURE)
    monkeypatch.setattr(hrd, "TARGET", str(target))

    patch = HttpsRedirectDatePatch()
    runner = Runner()

    assert patch.applicable(runner)[0] is True
    assert patch.state(runner) == PatchState.NOT_APPLIED

    patch.apply(runner)
    out = target.read_text()
    assert "sub _real_now" in out
    assert "$now // _real_now()" in out
    assert "my $time = time();" not in out
    assert patch.state(runner) == PatchState.APPLIED
    assert (str(target) + ".orig") in (a.target + a.detail for a in runner.actions) or \
        (tmp_path / "ssl_call.pm.orig").exists()

    patch.remove(runner)
    assert target.read_text() == _FIXTURE
    assert patch.state(runner) == PatchState.NOT_APPLIED


def test_account_startdate_components_well_formed():
    patch = AccountStartdatePatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["hook_script", "hook_registration"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_account_startdate_not_applicable_off_cpanel(tmp_path, monkeypatch):
    # With no manage_hooks present the patch must be NOT_APPLICABLE, never crash.
    monkeypatch.setattr(asd, "MANAGE_HOOKS", str(tmp_path / "nope" / "manage_hooks"))
    patch = AccountStartdatePatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is False
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_account_startdate_script_roundtrip(tmp_path, monkeypatch):
    # The hook_script component writes the managed asset (carrying its marker)
    # and removes it cleanly; the registration component is stubbed via a fake
    # manage_hooks so no real cPanel is required.
    script = tmp_path / "hook.pl"
    monkeypatch.setattr(asd, "HOOK_PATH", str(script))

    patch = AccountStartdatePatch()
    runner = Runner()
    comp = {c.key: c for c in patch.components()}["hook_script"]

    assert comp.present(runner) is False
    comp.apply(runner)
    assert comp.present(runner) is True
    assert asd.MARKER in script.read_text()
    comp.remove(runner)
    assert comp.present(runner) is False
    assert not script.exists()


def test_https_redirect_date_refuses_changed_build(tmp_path, monkeypatch):
    # If the vendor function no longer matches, the patch is NOT_APPLICABLE and
    # never tries to edit a file it doesn't understand.
    target = tmp_path / "ssl_call.pm"
    target.write_text("package X;\nsub something { 1 }\n1;\n")
    monkeypatch.setattr(hrd, "TARGET", str(target))

    patch = HttpsRedirectDatePatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is False
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_pdns_upcp_components_well_formed():
    patch = PdnsUpcpRemovalPatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["guard", "pdns_pkg"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_pdns_upcp_not_applicable_off_cpanel(tmp_path, monkeypatch):
    # No setupnameserver script -> not a cPanel host -> NOT_APPLICABLE, never crash.
    monkeypatch.setattr(puc, "SETUP_NAMESERVER", str(tmp_path / "nope"))
    patch = PdnsUpcpRemovalPatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is False
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_pdns_upcp_not_applicable_on_non_powerdns(tmp_path, monkeypatch):
    # A bind/nsd/disabled host must never be switched to PowerDNS by this patch.
    setup = tmp_path / "setupnameserver"
    setup.write_text("#!/bin/sh\n")
    cfg = tmp_path / "cpanel.config"
    cfg.write_text("local_nameserver_type=bind\n")
    monkeypatch.setattr(puc, "SETUP_NAMESERVER", str(setup))
    monkeypatch.setattr(puc, "CPANEL_CONFIG", str(cfg))
    patch = PdnsUpcpRemovalPatch()
    runner = Runner()
    ok, reason = patch.applicable(runner)
    assert ok is False and "powerdns" in reason
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def _pdns_env(tmp_path, monkeypatch):
    """A PowerDNS-configured cPanel host with tunable package presence."""
    setup = tmp_path / "setupnameserver"
    setup.write_text("#!/bin/sh\n")
    cfg = tmp_path / "cpanel.config"
    cfg.write_text("local_nameserver_type=powerdns\n")
    guard = tmp_path / "pdns-guard"
    monkeypatch.setattr(puc, "SETUP_NAMESERVER", str(setup))
    monkeypatch.setattr(puc, "CPANEL_CONFIG", str(cfg))
    monkeypatch.setattr(puc, "GUARD", str(guard))
    return guard


def test_pdns_upcp_state_machine(tmp_path, monkeypatch):
    guard = _pdns_env(tmp_path, monkeypatch)
    installed = {"v": True}
    monkeypatch.setattr(puc, "_pdns_installed", lambda r: installed["v"])

    patch = PdnsUpcpRemovalPatch()
    runner = Runner()

    # Un-armed host (pdns installed, no guard) must NOT auto-arm itself.
    assert patch.state(runner) == PatchState.NOT_APPLIED

    # Armed + package present -> fully APPLIED.
    guard.write_text("armed")
    assert patch.state(runner) == PatchState.APPLIED

    # upcp removes the package: guard survives, package gone -> DRIFTED,
    # which is exactly what makes the post-upcp `reapply` heal it.
    installed["v"] = False
    assert patch.state(runner) == PatchState.DRIFTED

    # Disarmed (guard also gone) -> back to NOT_APPLIED.
    guard.unlink()
    assert patch.state(runner) == PatchState.NOT_APPLIED


def test_pdns_upcp_apply_plans_guard_then_reinstall(tmp_path, monkeypatch):
    # A dry-run apply on a host whose package was removed must plan BOTH the
    # guard marker and the setupnameserver reinstall, and touch nothing.
    guard = _pdns_env(tmp_path, monkeypatch)
    monkeypatch.setattr(puc, "_pdns_installed", lambda r: False)

    patch = PdnsUpcpRemovalPatch()
    runner = Runner(dry_run=True)
    patch.apply(runner)

    targets = [a.target for a in runner.actions]
    assert str(guard) in targets
    assert any("setupnameserver --force powerdns" in t for t in targets)
    assert not guard.exists()  # dry-run changed nothing


def test_pdns_upcp_remove_never_uninstalls_dns(tmp_path, monkeypatch):
    # Removing the patch disarms the guard but must never uninstall PowerDNS.
    guard = _pdns_env(tmp_path, monkeypatch)
    guard.write_text("armed")
    monkeypatch.setattr(puc, "_pdns_installed", lambda r: True)

    patch = PdnsUpcpRemovalPatch()
    runner = Runner()
    patch.remove(runner)

    assert not guard.exists()                       # guard disarmed
    kinds = [(a.kind, a.target) for a in runner.actions]
    assert all(a.kind != "run" for a in runner.actions), kinds  # nothing executed
    assert patch.state(runner) == PatchState.NOT_APPLIED


# --------------------------------------------------------------------------- #
# self-heal plumbing: enrollment + PATH hardening (the 0.3.1 fixes)
# --------------------------------------------------------------------------- #
def test_enrollment_roundtrip(tmp_path, monkeypatch):
    from cpanel_doctor.core import enrollment

    monkeypatch.setattr(enrollment, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(enrollment, "ENROLLED_FILE", str(tmp_path / "state" / "enrolled"))

    assert enrollment.enrolled_ids() == set()          # missing file -> empty
    enrollment.enroll("https-redirect-date")
    enrollment.enroll("pg-cpses")
    enrollment.enroll("https-redirect-date")            # idempotent
    assert enrollment.enrolled_ids() == {"https-redirect-date", "pg-cpses"}
    assert enrollment.is_enrolled("pg-cpses")
    enrollment.unenroll("pg-cpses")
    assert enrollment.enrolled_ids() == {"https-redirect-date"}
    # hand-editable: blanks and comments ignored
    (tmp_path / "state" / "enrolled").write_text("# note\n\n  foo  \nbar\n")
    assert enrollment.enrolled_ids() == {"foo", "bar"}


def test_runner_path_includes_sbin(monkeypatch):
    # The post-upcp hook's minimal PATH omits sbin; Runner must add it back so
    # tools like `ip`/`systemctl` resolve. This is the crash root-cause fix.
    import shutil
    from cpanel_doctor.core import runner as runner_mod

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = runner_mod._augmented_env()
    parts = env["PATH"].split(":")
    assert "/sbin" in parts and "/usr/sbin" in parts
    # A capture() for an sbin tool should now actually find it (if present here).
    if shutil.which("ip", path=env["PATH"]):
        assert runner_mod.Runner.capture(["ip", "-V"]).returncode == 0


def test_https_redirect_date_is_single_component():
    # Single component => can only be APPLIED/NOT_APPLIED, never DRIFTED, which is
    # why the old DRIFTED-only reapply never healed it. Enrollment now covers it.
    assert len(HttpsRedirectDatePatch().components()) == 1
