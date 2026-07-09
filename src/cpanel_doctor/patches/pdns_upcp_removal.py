"""Patch: PowerDNS silently uninstalled by a cPanel update (upcp).

Problem
-------
On a host whose authoritative nameserver is PowerDNS
(``local_nameserver_type=powerdns`` in ``/var/cpanel/cpanel.config``), a cPanel
update (``/scripts/upcp``) can decide the ``cpanel-pdns`` package is *unneeded*
and remove it mid-run. The upcp log shows it plainly::

    Uninstalling unneeded rpms: cpanel-pdns
    warning: /etc/pdns/pdns.conf saved as /etc/pdns/pdns.conf.rpmsave

When that happens the package, the ``/usr/sbin/pdns_server`` binary and the
``pdns.service`` systemd unit all disappear, nothing is left listening on port
53, and **every** domain served by this box stops resolving. Because the whole
package is gone (not just stopped), ``chkservd`` cannot bring it back — it can
restart a stopped service, not reinstall a deleted one — so the outage persists
until someone reinstalls PowerDNS by hand. This has been observed after major
version bumps (e.g. the jump to ``cpanel-pdns-…cp130``).

Fix
---
Reinstall and reconfigure PowerDNS with the officially supported command,
``/usr/local/cpanel/scripts/setupnameserver --force powerdns``, which reinstalls
``cpanel-pdns``, regenerates ``/etc/pdns/pdns.conf``, re-enables and starts the
service and restores service monitoring. The local zone files in ``/var/named``
are left untouched, so all zones come back exactly as before.

The clever part is *when* this runs. cPanel Doctor already registers a post-upcp
self-heal hook that runs ``cpanel-doctor reapply`` after every update, and
``reapply`` re-applies patches that are **DRIFTED**. So this patch is modelled as
two components:

* ``guard``     — a small marker file under ``/var/lib/cpanel-doctor`` that upcp
                  never touches. It is the opt-in "arm the PowerDNS watchdog"
                  flag and, crucially, it *survives* the update.
* ``pdns_pkg``  — considered present only while armed **and** ``cpanel-pdns`` is
                  installed. upcp removing the package flips this to missing.

After an update that removed PowerDNS the guard is still present but the package
is gone → the patch reads **DRIFTED**, the post-upcp hook's ``reapply`` fires,
and ``setupnameserver --force powerdns`` brings DNS straight back. On a healthy
host both components are present (**APPLIED**); on an un-armed host both read
missing (**NOT APPLIED**), so the watchdog never arms itself without an explicit
``apply``.

Components
----------
1. ``guard``     opt-in marker; persists across upcp so a removal reads as DRIFTED
2. ``pdns_pkg``  ``cpanel-pdns`` installed; healed with ``setupnameserver --force``
"""
from __future__ import annotations

from typing import List, Tuple

from ..core import Component, Patch, Runner, RunnerError

SETUP_NAMESERVER = "/usr/local/cpanel/scripts/setupnameserver"
CPANEL_CONFIG = "/var/cpanel/cpanel.config"
GUARD = "/var/lib/cpanel-doctor/pdns-guard"
PDNS_PACKAGES = ("cpanel-pdns", "pdns")  # cp130+ ships cpanel-pdns; older builds, pdns

GUARD_CONTENT = (
    "# cPanel Doctor :: pdns-upcp-removal guard\n"
    "# This marker arms the PowerDNS self-heal. While it exists, a cPanel update\n"
    "# that uninstalls PowerDNS leaves this patch DRIFTED, so the post-upcp hook\n"
    "# reinstalls it via `setupnameserver --force powerdns`. Remove the patch with\n"
    "# `cpanel-doctor remove pdns-upcp-removal` to disarm (never uninstalls DNS).\n"
)


def _nameserver_type(r: Runner) -> str:
    """Value of ``local_nameserver_type`` in cpanel.config (empty if unknown)."""
    text = r.read(CPANEL_CONFIG) or ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "local_nameserver_type":
            return value.strip()
    return ""


def _pdns_installed(r: Runner) -> bool:
    for pkg in PDNS_PACKAGES:
        try:
            if r.capture(["rpm", "-q", pkg]).returncode == 0:
                return True
        except (OSError, ValueError):
            return False
    return False


class PdnsUpcpRemovalPatch(Patch):
    id = "pdns-upcp-removal"
    name = "PowerDNS uninstalled by cPanel updates (upcp)"
    summary = (
        "Reinstall PowerDNS automatically when a cPanel update removes the "
        "cpanel-pdns package and takes DNS down."
    )
    description = (
        "On a PowerDNS host, cPanel's upcp can classify cpanel-pdns as 'unneeded' and "
        "uninstall it mid-update ('Uninstalling unneeded rpms: cpanel-pdns'), deleting "
        "the package, the pdns_server binary and the pdns.service unit. Nothing then "
        "listens on port 53 and every domain on the server stops resolving; chkservd "
        "cannot help because the package is gone, not merely stopped. This patch arms a "
        "guard so that such a removal reads as DRIFTED and the post-upcp self-heal hook "
        "reinstalls PowerDNS via 'setupnameserver --force powerdns', regenerating the "
        "config, restarting the service and restoring monitoring. Zone files in "
        "/var/named are never touched."
    )
    caveats = (
        "Only arms on hosts already configured for PowerDNS "
        "(local_nameserver_type=powerdns); it never switches a bind/nsd/disabled host to "
        "PowerDNS. Healing runs 'setupnameserver --force powerdns', which reinstalls "
        "cpanel-pdns and restarts the pdns service (a brief DNS blip during reinstall). "
        "Removing the patch only disarms the guard — it never uninstalls PowerDNS."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(SETUP_NAMESERVER):
            return False, "setupnameserver not found (not a cPanel host?)"
        ns = _nameserver_type(r)
        if ns != "powerdns":
            return False, f"nameserver type is '{ns or 'unset'}', not powerdns"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "guard",
                "self-heal armed (marker survives upcp so a removal reads as DRIFTED)",
                self._guard_present,
                self._guard_apply,
                self._guard_remove,
            ),
            Component(
                "pdns_pkg",
                "PowerDNS (cpanel-pdns) installed and serving DNS",
                self._pkg_present,
                self._pkg_apply,
                self._pkg_remove,
            ),
        ]

    # guard: the persistent opt-in marker ----------------------------------
    def _guard_present(self, r: Runner) -> bool:
        return r.exists(GUARD)

    def _guard_apply(self, r: Runner) -> None:
        r.write(GUARD, GUARD_CONTENT, mode=0o644)

    def _guard_remove(self, r: Runner) -> None:
        r.remove(GUARD)

    # pdns_pkg: present only while armed, so an un-armed host stays NOT_APPLIED
    # and a post-upcp removal (guard present, package gone) reads as DRIFTED.
    def _pkg_present(self, r: Runner) -> bool:
        return r.exists(GUARD) and _pdns_installed(r)

    def _pkg_apply(self, r: Runner) -> None:
        # yum install + service restart can take a while; give it room.
        r.run([SETUP_NAMESERVER, "--force", "powerdns"], timeout=900)
        # Only assert the post-condition when we actually changed something.
        if not r.dry_run and not _pdns_installed(r):
            raise RunnerError("setupnameserver ran but cpanel-pdns is still not installed")

    def _pkg_remove(self, r: Runner) -> None:
        # Deliberately a no-op: disarming the watchdog must never take DNS down.
        r.note("pdns_pkg: leaving PowerDNS installed (remove only disarms the guard)")

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"
        if not _pdns_installed(r):
            return False, "cpanel-pdns is not installed"
        active = (r.capture(["systemctl", "is-active", "pdns"]).stdout or "").strip()
        if active != "active":
            return False, f"pdns service is not active (is-active={active or 'unknown'})"
        return True, "cpanel-pdns installed and pdns service active"
