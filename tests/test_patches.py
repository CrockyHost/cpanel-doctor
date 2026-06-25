"""Patch-system sanity tests (no root / no live cPanel required)."""
from __future__ import annotations

from cpanel_doctor.core import Patch, PatchState, Runner, load_patches, patches_by_id
from cpanel_doctor.patches import account_startdate as asd
from cpanel_doctor.patches import https_redirect_date as hrd
from cpanel_doctor.patches.account_startdate import AccountStartdatePatch
from cpanel_doctor.patches.https_redirect_date import HttpsRedirectDatePatch
from cpanel_doctor.patches.pg_cpses import PgCpsesPatch


def test_patches_discovered():
    patches = load_patches()
    assert patches, "no patches discovered"
    assert all(isinstance(p, Patch) for p in patches)
    registry = patches_by_id()
    assert "pg-cpses" in registry
    assert "https-redirect-date" in registry
    assert "account-startdate" in registry


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
