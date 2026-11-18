"""Patch-system sanity tests (no root / no live cPanel required)."""
from __future__ import annotations

import os

from cpanel_doctor.core import Action, Patch, PatchState, Runner, load_patches, patches_by_id
from cpanel_doctor.patches import account_startdate as asd
from cpanel_doctor.patches import https_redirect_date as hrd
from cpanel_doctor.patches import pdns_upcp_removal as puc
from cpanel_doctor.patches import mail_sni_sync as mss
from cpanel_doctor.patches import lshttpd_pidfile as lpf
from cpanel_doctor.patches import lsws_suspend_reload as lsr
from cpanel_doctor.patches import lsws_ssl_reload as lsl
from cpanel_doctor.patches import lastlogin_date as lld
from cpanel_doctor.patches.account_startdate import AccountStartdatePatch
from cpanel_doctor.patches.https_redirect_date import HttpsRedirectDatePatch
from cpanel_doctor.patches.pdns_upcp_removal import PdnsUpcpRemovalPatch
from cpanel_doctor.patches.mail_sni_sync import MailSniSyncPatch
from cpanel_doctor.patches.lshttpd_pidfile import LshttpdPidfilePatch
from cpanel_doctor.patches.lsws_suspend_reload import LswsSuspendReloadPatch
from cpanel_doctor.patches.lsws_ssl_reload import LswsSslReloadPatch
from cpanel_doctor.patches.pg_cpses import PgCpsesPatch
from cpanel_doctor.patches.lastlogin_date import LastloginDatePatch


def test_patches_discovered():
    patches = load_patches()
    assert patches, "no patches discovered"
    assert all(isinstance(p, Patch) for p in patches)
    registry = patches_by_id()
    assert "pg-cpses" in registry
    assert "https-redirect-date" in registry
    assert "account-startdate" in registry
    assert "pdns-upcp-removal" in registry
    assert "mail-sni-sync" in registry
    assert "lsws-suspend-reload" in registry
    assert "lshttpd-pidfile" in registry
    assert "lsws-ssl-reload" in registry
    assert "lastlogin-date" in registry


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


# --------------------------------------------------------------------------- #
# mail-sni-sync: mail ports must present the domain cert, not the hostname cert
# --------------------------------------------------------------------------- #
def test_mail_sni_components_well_formed():
    patch = MailSniSyncPatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["sni_synced"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_mail_sni_not_applicable_off_cpanel(tmp_path, monkeypatch):
    # No build_mail_sni script -> not a cPanel host -> NOT_APPLICABLE, never crash.
    monkeypatch.setattr(mss, "BUILD_MAIL_SNI", str(tmp_path / "nope"))
    patch = MailSniSyncPatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is False
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def _sni_env(tmp_path, monkeypatch, userdata: str, sni: str, ca_domains, covers=None,
             mailboxes=()):
    """A fake cPanel mail-SNI environment with tunable inputs.

    ``ca_domains`` is the set of vhosts whose apache_tls ``combined`` is treated
    as a real (CA-issued) certificate; anything else is considered self-signed.
    ``covers`` maps a vhost to extra names its certificate is valid for, which is
    how a parked domain rides on the main domain's SAN list. ``mailboxes`` is
    the set of domains given one email account (``~user/etc/<domain>/passwd``);
    cPanel only wires an addon/parked domain into mail SNI when it has one.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(mss, "_user_home", lambda user: str(home / user))
    build = tmp_path / "build_mail_sni"
    build.write_text("#!/bin/sh\n")
    dovecot = tmp_path / "dovecot"
    dovecot.mkdir()
    sni_conf = dovecot / "sni.conf"
    sni_conf.write_text(sni)
    userdata_file = tmp_path / "userdatadomains"
    userdata_file.write_text(userdata)
    apache_tls = tmp_path / "apache_tls"
    # Certificates are filed per *vhost*, not per domain -- a parked/addon domain
    # has no directory of its own. Mirroring that is the whole point here.
    for line in userdata.splitlines():
        rest = line.split(":", 1)[1] if ":" in line else ""
        fields = [f.strip().lower() for f in rest.split("==")]
        dtype = next((f for f in fields if f in {"main", "addon", "parked", "sub"}), "")
        if not dtype:
            continue
        vhost = fields[fields.index(dtype) + 1]
        (apache_tls / vhost).mkdir(parents=True, exist_ok=True)
        (apache_tls / vhost / "combined").write_text("cert")
        domain = line.split(":", 1)[0].strip().lower()
        if domain in mailboxes:
            etc = home / fields[0] / "etc" / domain
            etc.mkdir(parents=True, exist_ok=True)
            (etc / "passwd").write_text(f"info:$6$hash:1000:1000::/home/{fields[0]}/mail/{domain}/info:/bin/false\n")

    covers = covers or {}

    def _names(r, path):
        vhost = os.path.basename(os.path.dirname(path))
        return {vhost, f"*.{vhost}"} | set(covers.get(vhost, ()))

    monkeypatch.setattr(mss, "BUILD_MAIL_SNI", str(build))
    monkeypatch.setattr(mss, "SNI_CONF", str(sni_conf))
    monkeypatch.setattr(mss, "USERDATADOMAINS", str(userdata_file))
    monkeypatch.setattr(mss, "APACHE_TLS_DIR", str(apache_tls))
    monkeypatch.setattr(mss, "_is_ca_signed", lambda r, path: any(f"/{d}/" in path for d in ca_domains))
    monkeypatch.setattr(mss, "_cert_names", _names)


# A realistic slice: two mail-bearing main domains, a web-only sub, and a
# self-signed placeholder main. Only `good.example` is wired into mail SNI.
_USERDATA = (
    "good.example: alice==alice==main==good.example==/home/alice/public_html==1.2.3.4\n"
    "broken.example: bob==bob==main==broken.example==/home/bob/public_html==1.2.3.4\n"
    "landing.good.example: alice==alice==sub==good.example==/home/alice/l==1.2.3.4\n"
    "placeholder.example: root==root==main==placeholder.example==/home/x==1.2.3.4\n"
)
_SNI_ONLY_GOOD = (
    'local_name "good.example" {\n  ssl_server_cert_file = /x\n}\n'
    'local_name "*.good.example" {\n  ssl_server_cert_file = /x\n}\n'
)


def test_mail_sni_drift_when_ca_domain_missing(tmp_path, monkeypatch):
    # broken.example has a real cert but no SNI block -> patch reads NOT_APPLIED
    # (single component) and self_test names the offending domain.
    _sni_env(tmp_path, monkeypatch, _USERDATA, _SNI_ONLY_GOOD,
             ca_domains={"good.example", "broken.example"})
    patch = MailSniSyncPatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is True
    assert mss._domains_missing_from_sni(runner) == ["broken.example"]
    assert patch.state(runner) == PatchState.NOT_APPLIED
    ok, msg = patch.self_test(runner)
    assert ok is False and "broken.example" in msg


def test_mail_sni_ignores_subdomains_and_selfsigned(tmp_path, monkeypatch):
    # With broken.example also wired into SNI, nothing should remain missing:
    # the web-only `sub` domain and the self-signed `placeholder` main are both
    # correctly excluded, so the patch reads APPLIED (no false positives).
    sni = _SNI_ONLY_GOOD + 'local_name "broken.example" {\n  ssl_server_cert_file = /x\n}\n'
    _sni_env(tmp_path, monkeypatch, _USERDATA, sni,
             ca_domains={"good.example", "broken.example"})  # placeholder stays self-signed
    patch = MailSniSyncPatch()
    runner = Runner()
    assert mss._domains_missing_from_sni(runner) == []
    assert patch.state(runner) == PatchState.APPLIED


def test_mail_sni_apply_runs_build_and_is_dry_safe(tmp_path, monkeypatch):
    # A dry-run apply must plan the build_mail_sni rebuild and change nothing.
    _sni_env(tmp_path, monkeypatch, _USERDATA, _SNI_ONLY_GOOD,
             ca_domains={"good.example", "broken.example"})
    patch = MailSniSyncPatch()
    runner = Runner(dry_run=True)
    patch.apply(runner)
    ran = [a.target for a in runner.actions if a.kind == "run"]
    assert any("build_mail_sni" in t and "--rebuild_dovecot_sni_conf" in t for t in ran)


def test_mail_sni_remove_never_strips_certs(tmp_path, monkeypatch):
    # Removing the patch must not execute anything (it only stops self-heal).
    sni = _SNI_ONLY_GOOD + 'local_name "broken.example" {\n  ssl_server_cert_file = /x\n}\n'
    _sni_env(tmp_path, monkeypatch, _USERDATA, sni,
             ca_domains={"good.example", "broken.example"})
    patch = MailSniSyncPatch()
    runner = Runner()
    patch.remove(runner)
    assert all(a.kind != "run" for a in runner.actions)


# --------------------------------------------------------------------------- #
# lsws-suspend-reload: a suspended account must stop being served
# --------------------------------------------------------------------------- #

# A realistic `manage_hooks list` slice: our script under both account events,
# other vendors' hooks under the same and neighbouring events.
_HOOK_LISTING = """Whostmgr:
	Accounts::suspendacct:
		weight: 100
		hook: /usr/local/cpanel/3rdparty/wp-toolkit/bin/handle-event
		stage: pre
		--
		weight: 900
		hook: /usr/local/cpanel/scripts/cpanel_doctor_lsws_suspend_reload.sh
		stage: post
	Accounts::unsuspendacct:
		weight: 900
		hook: /usr/local/cpanel/scripts/cpanel_doctor_lsws_suspend_reload.sh
		stage: post
	Accounts::Create:
		weight: 100
		hook: /usr/local/cpanel/scripts/cpanel_doctor_account_startdate.pl
		stage: post
"""


def test_lsws_suspend_components_well_formed():
    patch = LswsSuspendReloadPatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["hook_script", "hook_suspend", "hook_unsuspend"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_lsws_suspend_hook_listing_parser():
    # Both account events must be recognised for our script, and only ours --
    # another vendor's hook under the same event must not be mistaken for it.
    events = lsr._parse_registered_events(_HOOK_LISTING, lsr.HOOK_PATH)
    assert events == {"Accounts::suspendacct", "Accounts::unsuspendacct"}
    other = lsr._parse_registered_events(
        _HOOK_LISTING, "/usr/local/cpanel/scripts/cpanel_doctor_account_startdate.pl"
    )
    assert other == {"Accounts::Create"}
    assert lsr._parse_registered_events(_HOOK_LISTING, "/nope") == set()


def test_lsws_suspend_not_applicable_without_litespeed(tmp_path, monkeypatch):
    # On an Apache host the include is re-read normally, so there is nothing to fix.
    hooks = tmp_path / "manage_hooks"
    hooks.write_text("#!/bin/sh\n")
    monkeypatch.setattr(lsr, "MANAGE_HOOKS", str(hooks))
    monkeypatch.setattr(lsr, "LSWSCTRL", str(tmp_path / "nope"))
    patch = LswsSuspendReloadPatch()
    runner = Runner()
    ok, reason = patch.applicable(runner)
    assert ok is False and "LiteSpeed" in reason
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_lsws_suspend_not_applicable_off_cpanel(tmp_path, monkeypatch):
    monkeypatch.setattr(lsr, "MANAGE_HOOKS", str(tmp_path / "nope"))
    patch = LswsSuspendReloadPatch()
    assert patch.applicable(Runner())[0] is False


def test_lsws_suspend_script_roundtrip(tmp_path, monkeypatch):
    # The managed script is written with its marker and removed cleanly.
    script = tmp_path / "reload.sh"
    monkeypatch.setattr(lsr, "HOOK_PATH", str(script))

    patch = LswsSuspendReloadPatch()
    runner = Runner()
    comp = {c.key: c for c in patch.components()}["hook_script"]

    assert comp.present(runner) is False
    comp.apply(runner)
    assert comp.present(runner) is True
    body = script.read_text()
    assert lsr.MARKER in body
    assert "MODE=graceful" in body          # zero-downtime default
    assert "flock" in body                  # bulk suspensions must not stack restarts
    comp.remove(runner)
    assert comp.present(runner) is False


def test_lsws_suspend_dry_run_registers_both_events(tmp_path, monkeypatch):
    # A dry-run apply must plan the script plus BOTH hook registrations, and
    # touch nothing on disk.
    script = tmp_path / "reload.sh"
    hooks = tmp_path / "manage_hooks"
    hooks.write_text("#!/bin/sh\n")
    monkeypatch.setattr(lsr, "HOOK_PATH", str(script))
    monkeypatch.setattr(lsr, "MANAGE_HOOKS", str(hooks))
    monkeypatch.setattr(lsr, "_registered_events", lambda r, p: set())

    patch = LswsSuspendReloadPatch()
    runner = Runner(dry_run=True)
    patch.apply(runner)

    ran = [a.target for a in runner.actions if a.kind == "run"]
    assert any("Accounts::suspendacct" in t for t in ran)
    assert any("Accounts::unsuspendacct" in t for t in ran)
    assert not script.exists()


def test_lsws_suspend_detects_server_older_than_suspensions(tmp_path, monkeypatch):
    # The signature of the bug: LiteSpeed started before the suspensions file was
    # last written, so it has never read the current suspensions.
    conf = tmp_path / "account_suspensions.conf"
    conf.write_text('<Directory "/home/bob">\n</Directory>\n')
    monkeypatch.setattr(lsr, "SUSPENSIONS_CONF", str(conf))
    runner = Runner()

    mtime = conf.stat().st_mtime
    monkeypatch.setattr(lsr, "_lsws_start_epoch", lambda r: int(mtime) - 3600)
    assert lsr._stale_suspensions(runner) is True      # server older -> stale
    monkeypatch.setattr(lsr, "_lsws_start_epoch", lambda r: int(mtime) + 3600)
    assert lsr._stale_suspensions(runner) is False     # restarted since -> current
    monkeypatch.setattr(lsr, "_lsws_start_epoch", lambda r: None)
    assert lsr._stale_suspensions(runner) is None      # undeterminable, never crash


def test_lsws_suspend_pidfile_read_from_lswsctrl(tmp_path, monkeypatch):
    # The PID path must be read from lswsctrl, not assumed to be /var/run.
    ctrl = tmp_path / "lswsctrl"
    ctrl.write_text("#!/bin/sh\nPIDFILE=/tmp/lshttpd/lshttpd.pid\n")
    monkeypatch.setattr(lsr, "LSWSCTRL", str(ctrl))
    assert lsr._lsws_pidfile(Runner()) == "/tmp/lshttpd/lshttpd.pid"


# --------------------------------------------------------------------------- #
# lshttpd-pidfile: the unit must point at the PID file lswsctrl really writes
# --------------------------------------------------------------------------- #
def _pidfile_env(tmp_path, monkeypatch, declared: str, real: str = "/tmp/lshttpd/lshttpd.pid"):
    """A LiteSpeed host whose unit declares ``declared`` while lswsctrl writes ``real``."""
    ctrl = tmp_path / "lswsctrl"
    ctrl.write_text(f"#!/bin/sh\nPIDFILE={real}\n")
    unit = tmp_path / "lshttpd.service"
    unit.write_text(f"[Service]\nType=forking\nPIDFile={declared}\n")
    dropin = tmp_path / "dropin.conf"
    monkeypatch.setattr(lpf, "LSWSCTRL", str(ctrl))
    monkeypatch.setattr(lpf, "DROPIN", str(dropin))
    monkeypatch.setattr(lpf, "_unit_fragment_path", lambda r: str(unit))
    return dropin


def test_lshttpd_pidfile_components_well_formed():
    patch = LshttpdPidfilePatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["pidfile_override"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_lshttpd_pidfile_not_applicable_when_unit_is_correct(tmp_path, monkeypatch):
    # A LiteSpeed build that ships the right path must be left alone.
    _pidfile_env(tmp_path, monkeypatch, declared="/tmp/lshttpd/lshttpd.pid")
    patch = LshttpdPidfilePatch()
    runner = Runner()
    ok, reason = patch.applicable(runner)
    assert ok is False and "already declares" in reason
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_lshttpd_pidfile_applicable_on_the_mismatch(tmp_path, monkeypatch):
    _pidfile_env(tmp_path, monkeypatch, declared="/var/run/lshttpd.pid")
    patch = LshttpdPidfilePatch()
    assert patch.applicable(Runner())[0] is True


def _stub_run(monkeypatch):
    """Record commands instead of executing them.

    apply()/remove() here really would run `systemctl daemon-reload`, which works
    for root but fails with "Interactive authentication required" on an
    unprivileged CI runner -- a test must not depend on the privileges it happens
    to have.
    """
    monkeypatch.setattr(
        Runner, "run",
        lambda self, cmd, timeout=120, check=True: self._record(Action("run", " ".join(cmd))),
    )


def test_lshttpd_pidfile_stays_applied_after_apply(tmp_path, monkeypatch):
    # Applying changes only the *effective* value; applicability is judged on the
    # vendor unit, so the patch must read APPLIED afterwards -- not NOT_APPLICABLE.
    dropin = _pidfile_env(tmp_path, monkeypatch, declared="/var/run/lshttpd.pid")
    effective = {"v": "/var/run/lshttpd.pid"}
    monkeypatch.setattr(lpf, "_effective_pidfile", lambda r: effective["v"])
    _stub_run(monkeypatch)

    patch = LshttpdPidfilePatch()
    runner = Runner()
    assert patch.state(runner) == PatchState.NOT_APPLIED

    patch.apply(runner)
    effective["v"] = "/tmp/lshttpd/lshttpd.pid"        # daemon-reload picked it up
    assert lpf.MARKER in dropin.read_text()
    assert "PIDFile=/tmp/lshttpd/lshttpd.pid" in dropin.read_text()
    # A drop-in is inert until systemd re-reads it, so apply must ask for that.
    assert any(a.kind == "run" and "daemon-reload" in a.target for a in runner.actions)
    assert patch.applicable(runner)[0] is True
    assert patch.state(runner) == PatchState.APPLIED

    patch.remove(runner)
    effective["v"] = "/var/run/lshttpd.pid"
    assert not dropin.exists()
    assert patch.state(runner) == PatchState.NOT_APPLIED


def test_lshttpd_pidfile_dropin_not_present_until_systemd_loads_it(tmp_path, monkeypatch):
    # A drop-in on disk that daemon-reload has not picked up must not count as
    # present, otherwise the patch would claim a fix that is not in effect.
    dropin = _pidfile_env(tmp_path, monkeypatch, declared="/var/run/lshttpd.pid")
    dropin.write_text(f"# {lpf.MARKER}\n[Service]\nPIDFile=/tmp/lshttpd/lshttpd.pid\n")
    monkeypatch.setattr(lpf, "_effective_pidfile", lambda r: "/var/run/lshttpd.pid")
    patch = LshttpdPidfilePatch()
    assert patch.state(Runner()) == PatchState.NOT_APPLIED


# --------------------------------------------------------------------------- #
# lsws-ssl-reload: a new certificate must actually be served
# --------------------------------------------------------------------------- #

# A realistic `manage_hooks list` slice: our script under both SSL events, plus a
# neighbouring event so the parser cannot simply match on the path alone.
_SSL_HOOK_LISTING = """Whostmgr:
	AutoSSL::installssl:
		weight: 100
		hook: /usr/local/cpanel/3rdparty/wp-toolkit/bin/handle-event
		stage: post
		--
		weight: 900
		hook: /usr/local/cpanel/scripts/cpanel_doctor_lsws_ssl_reload.sh
		stage: post
	SSL::installssl:
		weight: 900
		hook: /usr/local/cpanel/scripts/cpanel_doctor_lsws_ssl_reload.sh
		stage: post
	Accounts::suspendacct:
		weight: 900
		hook: /usr/local/cpanel/scripts/cpanel_doctor_lsws_suspend_reload.sh
		stage: post
"""


def test_lsws_ssl_components_well_formed():
    patch = LswsSslReloadPatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["hook_script", "hook_autossl", "hook_manual"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_lsws_ssl_hook_listing_parser():
    # Both SSL events must be recognised for our script, and the neighbouring
    # suspend hook must not be attributed to it.
    events = lsl._parse_registered_events(_SSL_HOOK_LISTING, lsl.HOOK_PATH)
    assert events == {"AutoSSL::installssl", "SSL::installssl"}
    other = lsl._parse_registered_events(
        _SSL_HOOK_LISTING, "/usr/local/cpanel/scripts/cpanel_doctor_lsws_suspend_reload.sh"
    )
    assert other == {"Accounts::suspendacct"}
    assert lsl._parse_registered_events(_SSL_HOOK_LISTING, "/nope") == set()


def test_lsws_ssl_not_applicable_without_litespeed(tmp_path, monkeypatch):
    # On an Apache host the cert is picked up by cPanel's own reload, nothing to fix.
    hooks = tmp_path / "manage_hooks"
    hooks.write_text("#!/bin/sh\n")
    monkeypatch.setattr(lsl, "MANAGE_HOOKS", str(hooks))
    monkeypatch.setattr(lsl, "LSWSCTRL", str(tmp_path / "nope"))
    patch = LswsSslReloadPatch()
    runner = Runner()
    ok, reason = patch.applicable(runner)
    assert ok is False and "LiteSpeed" in reason
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_lsws_ssl_not_applicable_off_cpanel(tmp_path, monkeypatch):
    monkeypatch.setattr(lsl, "MANAGE_HOOKS", str(tmp_path / "nope"))
    patch = LswsSslReloadPatch()
    assert patch.applicable(Runner())[0] is False


def test_lsws_ssl_script_roundtrip(tmp_path, monkeypatch):
    # The managed script is written with its marker and removed cleanly.
    script = tmp_path / "reload.sh"
    monkeypatch.setattr(lsl, "HOOK_PATH", str(script))

    patch = LswsSslReloadPatch()
    runner = Runner()
    comp = {c.key: c for c in patch.components()}["hook_script"]

    assert comp.present(runner) is False
    comp.apply(runner)
    assert comp.present(runner) is True
    body = script.read_text()
    assert lsl.MARKER in body
    assert "MODE=graceful" in body          # zero-downtime default
    # An AutoSSL run fires per vhost: the burst must coalesce into one restart,
    # which is what the non-blocking lock plus the debounce sleep achieve.
    assert "flock -n" in body
    assert "DEBOUNCE=" in body
    comp.remove(runner)
    assert comp.present(runner) is False


def test_lsws_ssl_dry_run_registers_both_events(tmp_path, monkeypatch):
    # A dry-run apply must plan the script plus BOTH hook registrations, and
    # touch nothing on disk.
    script = tmp_path / "reload.sh"
    hooks = tmp_path / "manage_hooks"
    hooks.write_text("#!/bin/sh\n")
    monkeypatch.setattr(lsl, "HOOK_PATH", str(script))
    monkeypatch.setattr(lsl, "MANAGE_HOOKS", str(hooks))
    monkeypatch.setattr(lsl, "_registered_events", lambda r, p: set())

    patch = LswsSslReloadPatch()
    runner = Runner(dry_run=True)
    patch.apply(runner)

    ran = [a.target for a in runner.actions if a.kind == "run"]
    assert any("AutoSSL::installssl" in t for t in ran)
    assert any("SSL::installssl" in t for t in ran)
    assert not script.exists()


def test_lsws_ssl_detects_server_older_than_newest_cert(tmp_path, monkeypatch):
    # The signature of the bug: LiteSpeed started before the certificate was
    # installed, so that vhost is still answered with the hostname certificate.
    tls = tmp_path / "apache_tls"
    (tls / "example.com").mkdir(parents=True)
    combined = tls / "example.com" / "combined"
    combined.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(lsl, "APACHE_TLS_DIR", str(tls))
    runner = Runner()

    mtime = combined.stat().st_mtime
    monkeypatch.setattr(lsl, "_lsws_start_epoch", lambda r: int(mtime) - 3600)
    assert lsl._stale_certs(runner) == (True, "example.com")   # server older -> stale
    monkeypatch.setattr(lsl, "_lsws_start_epoch", lambda r: int(mtime) + 3600)
    assert lsl._stale_certs(runner)[0] is False                # restarted since -> current
    monkeypatch.setattr(lsl, "_lsws_start_epoch", lambda r: None)
    assert lsl._stale_certs(runner)[0] is None                 # undeterminable, never crash


def test_lsws_ssl_newest_cert_picks_the_latest(tmp_path, monkeypatch):
    # With many vhosts only the most recently installed certificate matters --
    # that is the one the running server may not have read yet.
    import os

    tls = tmp_path / "apache_tls"
    for name in ("old.example", "new.example"):
        (tls / name).mkdir(parents=True)
        (tls / name / "combined").write_text("cert\n")
    os.utime(tls / "old.example" / "combined", (1000, 1000))
    os.utime(tls / "new.example" / "combined", (99000, 99000))
    # A directory with no certificate in it must be skipped, not crash.
    (tls / "pending.example").mkdir()
    monkeypatch.setattr(lsl, "APACHE_TLS_DIR", str(tls))

    mtime, domain = lsl._newest_cert(Runner())
    assert domain == "new.example" and mtime == 99000


def test_lsws_ssl_newest_cert_missing_dir_is_not_fatal(tmp_path, monkeypatch):
    # A host that has never installed a certificate has no apache_tls directory.
    monkeypatch.setattr(lsl, "APACHE_TLS_DIR", str(tmp_path / "nope"))
    assert lsl._newest_cert(Runner()) == (None, "")


def test_lsws_ssl_pidfile_read_from_lswsctrl(tmp_path, monkeypatch):
    # The PID path must be read from lswsctrl, not assumed to be /var/run.
    ctrl = tmp_path / "lswsctrl"
    ctrl.write_text("#!/bin/sh\nPIDFILE=/tmp/lshttpd/lshttpd.pid\n")
    monkeypatch.setattr(lsl, "LSWSCTRL", str(ctrl))
    assert lsl._lsws_pidfile(Runner()) == "/tmp/lshttpd/lshttpd.pid"


# A parked domain: no apache_tls directory of its own, served by the account's
# main-domain certificate whose SAN list covers it. Looking the certificate up by
# domain skipped these entirely -- the case that left mail.oxidev.ro handing out
# the server hostname certificate while the patch reported nothing wrong.
_PARKED_USERDATA = (
    "oxidev-systems.ro: oxidevsy==root==main==oxidev-systems.ro==/home/oxidevsy/public_html==1.2.3.4\n"
    "oxidev.ro: oxidevsy==root==parked==oxidev-systems.ro==/home/oxidevsy/public_html==1.2.3.4\n"
)
_SNI_ONLY_MAIN = 'local_name "oxidev-systems.ro" {\n  ssl_server_cert_file = /x\n}\n'


def test_mail_sni_candidate_domains_map_to_their_vhost(tmp_path, monkeypatch):
    userdata = tmp_path / "userdatadomains"
    userdata.write_text(_PARKED_USERDATA)
    monkeypatch.setattr(mss, "USERDATADOMAINS", str(userdata))
    assert mss._sni_candidate_domains(Runner()) == {
        "oxidev-systems.ro": mss._Candidate("oxidev-systems.ro", "oxidevsy", "main"),
        # parked -> the cert-bearing vhost
        "oxidev.ro": mss._Candidate("oxidev-systems.ro", "oxidevsy", "parked"),
    }


def test_mail_sni_detects_parked_domain_on_main_domain_cert(tmp_path, monkeypatch):
    # The regression: oxidev.ro is parked, has no apache_tls dir, and is covered by
    # the oxidev-systems.ro certificate's SAN list. It must be reported as missing.
    _sni_env(
        tmp_path, monkeypatch, _PARKED_USERDATA, _SNI_ONLY_MAIN,
        ca_domains={"oxidev-systems.ro"},
        covers={"oxidev-systems.ro": {"oxidev.ro", "*.oxidev.ro"}},
        mailboxes={"oxidev.ro"},
    )
    runner = Runner()
    assert mss._domains_missing_from_sni(runner) == ["oxidev.ro"]
    ok, msg = MailSniSyncPatch().self_test(runner)
    assert ok is False and "oxidev.ro" in msg


def test_mail_sni_ignores_domain_the_vhost_cert_does_not_cover(tmp_path, monkeypatch):
    # Same shape, but the certificate does not name the parked domain. Wiring it
    # into SNI would hand out a cert that fails hostname verification anyway, so
    # it must not be reported -- the fix is a new certificate, not a rebuild.
    _sni_env(
        tmp_path, monkeypatch, _PARKED_USERDATA, _SNI_ONLY_MAIN,
        ca_domains={"oxidev-systems.ro"},
        covers={},           # cert covers only oxidev-systems.ro
        mailboxes={"oxidev.ro"},
    )
    assert mss._domains_missing_from_sni(Runner()) == []


def test_mail_sni_name_covered_matches_one_level_wildcard():
    names = {"example.com", "*.example.com"}
    assert mss._name_covered("example.com", names)
    assert mss._name_covered("mail.example.com", names)
    assert not mss._name_covered("a.b.example.com", names)   # wildcard is one level
    assert not mss._name_covered("other.com", names)


# An addon domain with a CA certificate but no email accounts. cPanel's own
# dovecotSNI builder leaves such a domain out of mail SNI on purpose, so
# build_mail_sni can never add it. Flagging it left the patch NOT APPLIED for
# good and made every post-upcp reapply rebuild SNI, restart Dovecot/Exim and
# fail again (seen with iuliascs.ro, an addon of the whmcs account).
_ADDON_USERDATA = (
    "crocky.example: whmcs==root==main==crocky.example==/home/whmcs/public_html==1.2.3.4\n"
    "shop.example: whmcs==root==addon==shop.crocky.example==/home/whmcs/shop==1.2.3.4\n"
    "shop.crocky.example: whmcs==root==sub==crocky.example==/home/whmcs/shop==1.2.3.4\n"
)
_SNI_ONLY_CROCKY = (
    'local_name "crocky.example" {\n  ssl_server_cert_file = /x\n}\n'
    'local_name "*.crocky.example" {\n  ssl_server_cert_file = /x\n}\n'
)


def test_mail_sni_ignores_addon_without_mailboxes(tmp_path, monkeypatch):
    _sni_env(
        tmp_path, monkeypatch, _ADDON_USERDATA, _SNI_ONLY_CROCKY,
        ca_domains={"crocky.example", "shop.crocky.example"},
        covers={"shop.crocky.example": {"shop.example", "*.shop.example"}},
        mailboxes=(),
    )
    runner = Runner()
    assert mss._domains_missing_from_sni(runner) == []
    assert MailSniSyncPatch().state(runner) == PatchState.APPLIED


def test_mail_sni_flags_addon_once_it_has_a_mailbox(tmp_path, monkeypatch):
    # The same addon with one email account is exactly what cPanel *does* wire
    # into mail SNI, so its absence is real drift and must still be reported.
    _sni_env(
        tmp_path, monkeypatch, _ADDON_USERDATA, _SNI_ONLY_CROCKY,
        ca_domains={"crocky.example", "shop.crocky.example"},
        covers={"shop.crocky.example": {"shop.example", "*.shop.example"}},
        mailboxes={"shop.example"},
    )
    assert mss._domains_missing_from_sni(Runner()) == ["shop.example"]


def test_mail_sni_main_domain_needs_no_mailbox(tmp_path, monkeypatch):
    # An account's main domain gets mail SNI regardless of mailboxes (cPanel
    # includes every trueuserdomain), so a missing main stays flagged.
    _sni_env(tmp_path, monkeypatch, _USERDATA, _SNI_ONLY_GOOD,
             ca_domains={"good.example", "broken.example"}, mailboxes=())
    assert mss._domains_missing_from_sni(Runner()) == ["broken.example"]


def test_mail_sni_mailbox_count_ignores_blank_and_comment_lines(tmp_path, monkeypatch):
    etc = tmp_path / "bob" / "etc" / "b.example"
    etc.mkdir(parents=True)
    (etc / "passwd").write_text("\n# note\na:x:1:1::/h:/bin/false\nb:x:1:1::/h:/bin/false\n")
    monkeypatch.setattr(mss, "_user_home", lambda user: str(tmp_path / user))
    runner = Runner()
    assert mss._mailbox_count(runner, "bob", "b.example") == 2
    assert mss._mailbox_count(runner, "bob", "none.example") == 0
    assert mss._mailbox_count(runner, "", "b.example") == 0


# --------------------------------------------------------------------------- #
# lastlogin-date: ~/.lastlogin must carry the real login date
# --------------------------------------------------------------------------- #
def test_lastlogin_date_components_well_formed():
    patch = LastloginDatePatch()
    comps = patch.components()
    assert [c.key for c in comps] == ["fixer_script", "timer"]
    for c in comps:
        assert callable(c.present) and callable(c.apply) and callable(c.remove)


def test_lastlogin_date_not_applicable_off_cpanel(tmp_path, monkeypatch):
    monkeypatch.setattr(lld, "USERS_DIR", str(tmp_path / "nope"))
    patch = LastloginDatePatch()
    runner = Runner()
    assert patch.applicable(runner)[0] is False
    assert patch.state(runner) == PatchState.NOT_APPLICABLE


def test_lastlogin_date_refuses_changed_format(tmp_path, monkeypatch):
    # If cPanel stops writing "<ip> # <timestamp>", the script would misparse the
    # file, so the patch must stay out.
    (tmp_path / "users").mkdir()
    tiny = tmp_path / "Tiny.pm"
    tiny.write_text('package Cpanel::LastLogin::Tiny;\nmy $content = "$currenthost";\n1;\n')
    monkeypatch.setattr(lld, "USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setattr(lld, "TINY_PM", str(tiny))
    assert LastloginDatePatch().applicable(Runner())[0] is False


def test_lastlogin_date_script_roundtrip(tmp_path, monkeypatch):
    script = tmp_path / "fix.pl"
    monkeypatch.setattr(lld, "SCRIPT_PATH", str(script))
    comp = {c.key: c for c in LastloginDatePatch().components()}["fixer_script"]
    runner = Runner()

    assert comp.present(runner) is False
    comp.apply(runner)
    assert comp.present(runner) is True
    assert lld.MARKER in script.read_text()
    assert oct(script.stat().st_mode & 0o777) == "0o700"
    comp.remove(runner)
    assert comp.present(runner) is False


def test_lastlogin_date_units_carry_marker():
    from cpanel_doctor.core import asset_text

    service = asset_text(lld.SERVICE_NAME)
    timer = asset_text(lld.TIMER_NAME)
    assert lld.MARKER in service and lld.MARKER in timer
    assert lld.SCRIPT_PATH in service
    assert "OnUnitActiveSec=1min" in timer


def _run_fixer(tmp_path, *args):
    import shutil
    import subprocess

    import pytest

    perl = shutil.which("perl")
    if not perl:
        pytest.skip("perl not installed")
    from cpanel_doctor.core import asset_text

    script = tmp_path / "fix.pl"
    script.write_text(asset_text(lld.SCRIPT_ASSET))
    return subprocess.run([perl, str(script), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "TZ": "UTC"})


def test_lastlogin_date_fixer_copies_mtime_into_last_line(tmp_path):
    # cPanel stamped both logins with one frozen instant; the file's mtime is the
    # real time of the newest one. Only the last line changes, and the mtime is
    # put back so the next pass is a no-op.
    ll = tmp_path / ".lastlogin"
    ll.write_text("203.0.113.7 # 2026-10-08 20:06:22 +0300\n"
                  "198.51.100.9 # 2026-10-08 20:06:22 +0300")
    real = 1795029503  # 2026-11-18 19:18:23 UTC
    os.utime(ll, (real, real))

    res = _run_fixer(tmp_path, str(ll))
    assert res.returncode == 0, res.stderr
    assert "fixed=1" in res.stdout
    assert ll.read_text() == ("203.0.113.7 # 2026-10-08 20:06:22 +0300\n"
                              "198.51.100.9 # 2026-11-18 19:18:23 +0000")
    assert int(ll.stat().st_mtime) == real

    again = _run_fixer(tmp_path, str(ll))
    assert "ok=1" in again.stdout and "fixed" not in again.stdout


def test_lastlogin_date_fixer_leaves_correct_dates_alone(tmp_path):
    ll = tmp_path / ".lastlogin"
    body = "198.51.100.9 # 2026-11-18 19:18:23 +0000"
    ll.write_text(body)
    real = 1795029503 + 30  # written 30s after the stamp: within tolerance
    os.utime(ll, (real, real))
    res = _run_fixer(tmp_path, str(ll))
    assert "ok=1" in res.stdout
    assert ll.read_text() == body


def test_lastlogin_date_fixer_check_mode_writes_nothing(tmp_path):
    ll = tmp_path / ".lastlogin"
    body = "198.51.100.9 # 2026-10-08 20:06:22 +0300"
    ll.write_text(body)
    os.utime(ll, (1795029503, 1795029503))
    res = _run_fixer(tmp_path, "--check", str(ll))
    assert "would-fix=1" in res.stdout
    assert ll.read_text() == body


def test_lastlogin_date_fixer_skips_symlinks_and_garbage(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("198.51.100.9 # 2026-10-08 20:06:22 +0300")
    os.utime(victim, (1795029503, 1795029503))
    link = tmp_path / ".lastlogin"
    link.symlink_to(victim)
    junk = tmp_path / "junk"
    junk.write_text("not a lastlogin line")

    res = _run_fixer(tmp_path, str(link), str(junk))
    assert "skip=2" in res.stdout
    assert victim.read_text() == "198.51.100.9 # 2026-10-08 20:06:22 +0300"
    assert junk.read_text() == "not a lastlogin line"
