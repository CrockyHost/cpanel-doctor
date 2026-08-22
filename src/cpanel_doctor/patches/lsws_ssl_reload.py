"""Patch: a newly issued certificate is not served until LiteSpeed restarts.

Problem
-------
Installing a certificate — by hand in WHM, or automatically at the end of an
AutoSSL run — does everything right on the cPanel side: the certificate lands in
``/var/cpanel/ssl/apache_tls/<domain>/`` and ``/etc/apache2/conf/httpd.conf`` is
regenerated with the matching ``<VirtualHost ...:443>`` block. Nothing restarts
the web server.

LiteSpeed reads the Apache configuration, and the certificates it points at, only
at start/restart. A server process older than the certificate therefore keeps
serving the previous one — and a vhost that had *no* certificate before is not in
the running configuration at all, so the connection falls through to the default
listener and is answered with the **server hostname's** certificate. The visible
result is a freshly secured domain presenting a certificate issued for an
unrelated name, usually a SAN of the hostname certificate::

    $ openssl s_client -connect <ip>:443 -servername newsite.example
    subject=CN = cpcalendars.<server-hostname>

which browsers reject outright. If the hostname certificate is itself expired the
error is a confusing mix of "wrong name" and "expired".

The tell-tale signature is a LiteSpeed process older than the certificate::

    # ps -o lstart= -p $(cat /tmp/lshttpd/lshttpd.pid)   # Sat Aug 22 19:10:20
    # stat -c %y /var/cpanel/ssl/apache_tls/newsite.example/combined
                                                          2026-08-22 19:27:24

Certificates installed *before* that process started are served correctly, so
domains secured weeks apart behave differently and the fault looks intermittent.

Fix
---
Install a cPanel Standardized Hook on ``Whostmgr::AutoSSL::installssl`` and
``Whostmgr::SSL::installssl`` (post stage) that touches ``httpd.conf`` and reloads
LiteSpeed, so a new certificate goes live on its own.

AutoSSL fires its event once per vhost, so the naive hook would ask for one
restart per domain on every run. The installed script instead takes its lock
**non-blockingly** and exits when another invocation already holds it; the holder
sleeps ``DEBOUNCE`` seconds before reloading, absorbing the rest of the burst
while still reloading after the last certificate was written. A full AutoSSL run
collapses into a single restart. The worker is detached, so the hook never delays
the AutoSSL run or the WHM API response.

Components
----------
1. ``hook_script``    the reload script, installed on disk
2. ``hook_autossl``   Whostmgr::AutoSSL::installssl (post) registration
3. ``hook_manual``    Whostmgr::SSL::installssl (post) registration

None is a vendor file, so a cPanel update does not normally overwrite them; if any
goes missing the patch reads DRIFTED and is healed by ``reapply`` / the post-upcp
self-heal hook.
"""
from __future__ import annotations

import os
import re
import time as _time
from typing import List, Optional, Set, Tuple

from ..core import Component, Patch, Runner, asset_text

ASSET = "cpanel_doctor_lsws_ssl_reload.sh"
HOOK_PATH = "/usr/local/cpanel/scripts/cpanel_doctor_lsws_ssl_reload.sh"
MANAGE_HOOKS = "/usr/local/cpanel/bin/manage_hooks"
LSWSCTRL = "/usr/local/lsws/bin/lswsctrl"
APACHE_TLS_DIR = "/var/cpanel/ssl/apache_tls"
DEFAULT_PIDFILE = "/tmp/lshttpd/lshttpd.pid"

AUTOSSL_EVENT = "AutoSSL::installssl"
MANUAL_EVENT = "SSL::installssl"

# Unmistakable "this is our managed script" marker (a comment in the asset).
MARKER = "cpanel-doctor:lsws-ssl-reload"

# A hook listing groups entries under "<Category>:" then "  <Event>:" headers,
# with the script path on an indented "hook: ..." line beneath its event.
_EVENT_RE = re.compile(r"^\s+([A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)+):\s*$")
_HOOK_RE = re.compile(r"^\s+hook:\s*(\S+)\s*$")


def _hook_desc(event: str) -> List[str]:
    return ["--category", "Whostmgr", "--event", event, "--stage", "post"]


def _parse_registered_events(listing: str, path: str) -> Set[str]:
    """The events under which ``path`` appears in a ``manage_hooks list`` dump."""
    events: Set[str] = set()
    current = ""
    for line in listing.splitlines():
        m = _EVENT_RE.match(line)
        if m:
            current = m.group(1)
            continue
        m = _HOOK_RE.match(line)
        if m and current and m.group(1) == path:
            events.add(current)
    return events


def _registered_events(r: Runner, path: str) -> Set[str]:
    """The hook events under which ``path`` is currently registered."""
    if not r.exists(MANAGE_HOOKS):
        return set()
    return _parse_registered_events(r.capture([MANAGE_HOOKS, "list"]).stdout or "", path)


def _lsws_pidfile(r: Runner) -> str:
    """Where lswsctrl records the running server's PID (it is not /var/run)."""
    text = r.read(LSWSCTRL) or ""
    m = re.search(r"^PIDFILE=(\S+)", text, re.MULTILINE)
    return m.group(1) if m else DEFAULT_PIDFILE


def _lsws_start_epoch(r: Runner) -> Optional[int]:
    """Wall-clock start time of the running LiteSpeed server, or None."""
    pid = (r.read(_lsws_pidfile(r)) or "").strip()
    if not pid.isdigit():
        return None
    res = r.capture(["ps", "-o", "etimes=", "-p", pid])
    elapsed = (res.stdout or "").strip()
    if not elapsed.isdigit():
        return None
    return int(_time.time()) - int(elapsed)


def _newest_cert(r: Runner) -> Tuple[Optional[float], str]:
    """Mtime and domain of the most recently installed vhost certificate."""
    newest: Optional[float] = None
    domain = ""
    try:
        entries = os.listdir(APACHE_TLS_DIR)
    except OSError:
        return None, ""
    for name in entries:
        combined = os.path.join(APACHE_TLS_DIR, name, "combined")
        try:
            mtime = os.path.getmtime(combined)
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest, domain = mtime, name
    return newest, domain


def _stale_certs(r: Runner) -> Tuple[Optional[bool], str]:
    """Whether LiteSpeed started before the newest certificate was installed.

    ``True`` means at least one certificate has been installed that the running
    server has not read — the exact condition in which a secured domain is still
    answered with the hostname certificate. ``None`` when it cannot be determined.
    """
    started = _lsws_start_epoch(r)
    newest, domain = _newest_cert(r)
    if started is None or newest is None:
        return None, ""
    return newest > started, domain


class LswsSslReloadPatch(Patch):
    id = "lsws-ssl-reload"
    name = "New certificates not served until LiteSpeed restarts"
    summary = (
        "Reload LiteSpeed when a certificate is installed, so a newly secured "
        "domain stops being answered with the server hostname's certificate."
    )
    description = (
        "Installing a certificate — manually in WHM or at the end of an AutoSSL run — "
        "writes it under /var/cpanel/ssl/apache_tls/<domain>/ and regenerates httpd.conf, "
        "but nothing restarts the web server. LiteSpeed reads the Apache configuration and "
        "its certificates only at start/restart, so a server older than the certificate "
        "keeps serving the previous one; a vhost that had no certificate at all is missing "
        "from the running configuration entirely and falls through to the default listener, "
        "answering with the server hostname's certificate under a completely unrelated name. "
        "Certificates installed before the running process started work fine, which makes "
        "the fault look intermittent. This patch installs Whostmgr::AutoSSL::installssl and "
        "Whostmgr::SSL::installssl (post) hooks that touch httpd.conf and reload LiteSpeed. "
        "AutoSSL fires per vhost, so the script coalesces a burst into one restart: it takes "
        "its lock non-blockingly and exits if another invocation holds it, while that holder "
        "waits DEBOUNCE seconds before reloading."
    )
    caveats = (
        "The hook defaults to MODE=graceful: no downtime, but the outgoing LiteSpeed "
        "instance keeps draining with the old certificate, so a new one is fully live after "
        "roughly a minute rather than instantly. Set MODE=full in the installed script for "
        "an immediate switch at the cost of a few seconds of downtime for every site on the "
        "server — and apply the lshttpd-pidfile patch first, or that restart blocks for 90s "
        "and leaves the unit failed. Reloading is deferred by DEBOUNCE (20s) to absorb an "
        "AutoSSL burst, so even in MODE=full a certificate goes live a few seconds after it "
        "is issued, not the same instant. Only covers certificates installed AFTER it is "
        "applied; run the self-test to see whether the running server is already behind."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(MANAGE_HOOKS):
            return False, "manage_hooks not found (not a cPanel host?)"
        if not r.exists(LSWSCTRL):
            return False, "LiteSpeed not installed (Apache reloads itself on cert install)"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "hook_script",
                "LiteSpeed reload script run after a certificate is installed",
                self._script_present,
                self._script_apply,
                self._script_remove,
            ),
            Component(
                "hook_autossl",
                f"Whostmgr::{AUTOSSL_EVENT} (post) hook registered with manage_hooks",
                lambda r: AUTOSSL_EVENT in _registered_events(r, HOOK_PATH),
                lambda r: self._reg_apply(r, AUTOSSL_EVENT),
                lambda r: self._reg_remove(r, AUTOSSL_EVENT),
            ),
            Component(
                "hook_manual",
                f"Whostmgr::{MANUAL_EVENT} (post) hook registered with manage_hooks",
                lambda r: MANUAL_EVENT in _registered_events(r, HOOK_PATH),
                lambda r: self._reg_apply(r, MANUAL_EVENT),
                lambda r: self._reg_remove(r, MANUAL_EVENT),
            ),
        ]

    # hook_script ------------------------------------------------------------
    def _script_present(self, r: Runner) -> bool:
        return MARKER in (r.read(HOOK_PATH) or "")

    def _script_apply(self, r: Runner) -> None:
        r.write(HOOK_PATH, asset_text(ASSET), mode=0o700)

    def _script_remove(self, r: Runner) -> None:
        r.remove(HOOK_PATH)

    # hook registrations -----------------------------------------------------
    def _reg_apply(self, r: Runner, event: str) -> None:
        r.run(
            [MANAGE_HOOKS, "add", "script", HOOK_PATH, "--manual", *_hook_desc(event)],
            check=False,
        )

    def _reg_remove(self, r: Runner, event: str) -> None:
        r.run(
            [MANAGE_HOOKS, "delete", "script", HOOK_PATH, "--manual", *_hook_desc(event)],
            check=False,
        )

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        """Confirm the hook is wired up and the running server is not already behind."""
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"

        if not self._script_present(r):
            return False, "reload script not installed"
        events = _registered_events(r, HOOK_PATH)
        missing = [e for e in (AUTOSSL_EVENT, MANUAL_EVENT) if e not in events]
        if missing:
            return False, "hook not registered on " + ", ".join(missing)

        res = r.capture(["/bin/bash", "-n", HOOK_PATH])
        if res.returncode != 0:
            return False, f"reload script has a syntax error: {(res.stderr or '').strip()[:200]}"

        # The failure this patch exists for: a server older than the newest
        # certificate has not read it and is still presenting the old one.
        stale, domain = _stale_certs(r)
        if stale is True:
            return False, (
                f"LiteSpeed started before the certificate for {domain} was installed — "
                "that domain may still be served the hostname certificate; "
                "reload LiteSpeed once to catch up"
            )
        if stale is None:
            return True, "hook installed on AutoSSL + manual install (LSWS age undetermined)"
        return True, "hook installed on AutoSSL + manual install; LiteSpeed has current certificates"
