"""Patch: suspended accounts keep being served by LiteSpeed.

Problem
-------
Suspending an account in WHM does everything on the cPanel side correctly: it
creates ``/var/cpanel/suspended/<user>``, sets ``SUSPENDED=1`` in
``/var/cpanel/users/<user>`` and regenerates
``/etc/apache2/conf.d/includes/account_suspensions.conf`` with a redirect block::

    <Directory "/home/<user>">
        AllowOverride none
        RedirectMatch ^/(?!cgi-sys/suspendedpage.cgi).* /cgi-sys/suspendedpage.cgi
    </Directory>

On a LiteSpeed host that block is never read. LSWS parses the Apache
configuration only at start/restart, and its change detection watches the
*parent* file, ``/etc/apache2/conf/httpd.conf`` — not the files it ``Include``s.
Suspending an account rewrites only the include, so nothing in the chain tells
LiteSpeed to re-read anything, and **the site stays online while WHM reports the
account suspended**. It can stay that way for days, until LSWS restarts for some
unrelated reason.

The tell-tale signature is a LiteSpeed process older than the suspensions file::

    # ps -o lstart= -p $(cat /tmp/lshttpd/lshttpd.pid)   # Thu Aug 20 13:47:14
    # stat -c %y /etc/apache2/conf.d/includes/account_suspensions.conf
                                                          2026-08-22 13:00:32

Accounts suspended *before* that process started redirect correctly, because
their blocks were in the file when it parsed the config; anything suspended after
is still served. Two accounts suspended weeks apart behaving differently is the
clearest symptom.

Fix
---
Install a cPanel Standardized Hook on ``Whostmgr::Accounts::suspendacct`` and
``::unsuspendacct`` (post stage) that touches ``httpd.conf`` and reloads
LiteSpeed, so a suspension takes effect on its own.

The hook defaults to ``MODE=graceful``: zero downtime, but the outgoing instance
keeps draining and answering with the old configuration until
``gracefulRestartTimeout``, so a suspension lands after roughly a minute rather
than instantly. Setting ``MODE=full`` in the installed script trades that for an
immediate ``systemctl restart lshttpd`` at the cost of a few seconds of downtime
for every site on the server — see the ``lshttpd-pidfile`` patch, without which
that restart blocks for 90s and leaves the unit ``failed``.

Both restart styles briefly run two LiteSpeed instances at once; because they
share the listening sockets (``SO_REUSEPORT``) with different configurations, the
same URL can answer 200 or 302 seemingly at random while the old one drains. The
hook holds an ``flock`` so bulk suspensions cannot stack more of them up.

Components
----------
1. ``hook_script``     the reload script, installed on disk
2. ``hook_suspend``    Whostmgr::Accounts::suspendacct (post) registration
3. ``hook_unsuspend``  Whostmgr::Accounts::unsuspendacct (post) registration

None is a vendor file, so a cPanel update does not normally overwrite them; if
any goes missing the patch reads DRIFTED and is healed by ``reapply`` / the
post-upcp self-heal hook.
"""
from __future__ import annotations

import re
import time as _time
from typing import List, Optional, Set, Tuple

from ..core import Component, Patch, Runner, asset_text

ASSET = "cpanel_doctor_lsws_suspend_reload.sh"
HOOK_PATH = "/usr/local/cpanel/scripts/cpanel_doctor_lsws_suspend_reload.sh"
MANAGE_HOOKS = "/usr/local/cpanel/bin/manage_hooks"
LSWSCTRL = "/usr/local/lsws/bin/lswsctrl"
SUSPENSIONS_CONF = "/etc/apache2/conf.d/includes/account_suspensions.conf"
SUSPENDED_DIR = "/var/cpanel/suspended"
DEFAULT_PIDFILE = "/tmp/lshttpd/lshttpd.pid"

SUSPEND_EVENT = "Accounts::suspendacct"
UNSUSPEND_EVENT = "Accounts::unsuspendacct"


def _hook_desc(event: str) -> List[str]:
    return ["--category", "Whostmgr", "--event", event, "--stage", "post"]


# Unmistakable "this is our managed script" marker (a comment in the asset).
MARKER = "cpanel-doctor:lsws-suspend-reload"

# A hook listing groups entries under "<Category>:" then "  <Event>:" headers,
# with the script path on an indented "hook: ..." line beneath its event.
_EVENT_RE = re.compile(r"^\s+([A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)+):\s*$")
_HOOK_RE = re.compile(r"^\s+hook:\s*(\S+)\s*$")


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


def _stale_suspensions(r: Runner) -> Optional[bool]:
    """Whether LiteSpeed started before the suspensions file was last written.

    ``True`` means at least one suspend/unsuspend has happened that the running
    server has not read — the exact condition in which a suspended site is still
    being served. ``None`` when it cannot be determined.
    """
    import os

    started = _lsws_start_epoch(r)
    if started is None or not r.exists(SUSPENSIONS_CONF):
        return None
    try:
        return os.path.getmtime(SUSPENSIONS_CONF) > started
    except OSError:
        return None


class LswsSuspendReloadPatch(Patch):
    id = "lsws-suspend-reload"
    name = "Suspended accounts still served by LiteSpeed"
    summary = (
        "Reload LiteSpeed when an account is suspended or unsuspended, so the "
        "suspension actually takes effect instead of the site staying online."
    )
    description = (
        "Suspending an account regenerates /etc/apache2/conf.d/includes/"
        "account_suspensions.conf, but LiteSpeed reads the Apache configuration only at "
        "start/restart and watches httpd.conf rather than the files it includes. Nothing "
        "restarts the web server on suspend, so the redirect to suspendedpage.cgi is never "
        "loaded and the site keeps serving while WHM shows the account suspended — often "
        "for days. Accounts suspended before the running LSWS process started behave "
        "correctly, which makes the bug look random. This patch installs a "
        "Whostmgr::Accounts::suspendacct / ::unsuspendacct (post) hook that touches "
        "httpd.conf and reloads LiteSpeed, serialized with a lock so bulk suspensions do "
        "not stack up overlapping restarts."
    )
    caveats = (
        "The hook defaults to MODE=graceful: no downtime, but the outgoing LiteSpeed "
        "instance keeps draining and answering with the old configuration, so a "
        "suspension becomes fully effective after roughly a minute rather than instantly. "
        "Set MODE=full in the installed script for an immediate effect at the cost of a "
        "few seconds of downtime for every site on the server per suspend — and apply the "
        "lshttpd-pidfile patch first, or that restart blocks for 90s and leaves the unit "
        "failed. Only fixes suspensions performed AFTER it is applied; run the self-test "
        "to see whether the running server is already behind."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(MANAGE_HOOKS):
            return False, "manage_hooks not found (not a cPanel host?)"
        if not r.exists(LSWSCTRL):
            return False, "LiteSpeed not installed (Apache re-reads its own includes)"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "hook_script",
                "LiteSpeed reload script run after a suspend/unsuspend",
                self._script_present,
                self._script_apply,
                self._script_remove,
            ),
            Component(
                "hook_suspend",
                f"Whostmgr::{SUSPEND_EVENT} (post) hook registered with manage_hooks",
                lambda r: SUSPEND_EVENT in _registered_events(r, HOOK_PATH),
                lambda r: self._reg_apply(r, SUSPEND_EVENT),
                lambda r: self._reg_remove(r, SUSPEND_EVENT),
            ),
            Component(
                "hook_unsuspend",
                f"Whostmgr::{UNSUSPEND_EVENT} (post) hook registered with manage_hooks",
                lambda r: UNSUSPEND_EVENT in _registered_events(r, HOOK_PATH),
                lambda r: self._reg_apply(r, UNSUSPEND_EVENT),
                lambda r: self._reg_remove(r, UNSUSPEND_EVENT),
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
        missing = [e for e in (SUSPEND_EVENT, UNSUSPEND_EVENT) if e not in events]
        if missing:
            return False, "hook not registered on " + ", ".join(missing)

        res = r.capture(["/bin/bash", "-n", HOOK_PATH])
        if res.returncode != 0:
            return False, f"reload script has a syntax error: {(res.stderr or '').strip()[:200]}"

        # The failure this patch exists for: a server older than the suspensions
        # file has not read the current suspensions and is still serving them.
        stale = _stale_suspensions(r)
        if stale is True:
            return False, (
                "LiteSpeed started before account_suspensions.conf was last written — "
                "suspended sites may still be served; reload LiteSpeed once to catch up"
            )
        if stale is None:
            return True, "hook installed on suspend + unsuspend (LSWS age undetermined)"
        return True, "hook installed on suspend + unsuspend; LiteSpeed has current suspensions"
