"""Patch: the LiteSpeed systemd unit points at a PID file that is never written.

Problem
-------
LiteSpeed ships ``lshttpd.service`` declaring::

    Type=forking
    PIDFile=/var/run/lshttpd.pid

but ``/usr/local/lsws/bin/lswsctrl`` actually records the server's PID somewhere
else — ``PIDFILE=/tmp/lshttpd/lshttpd.pid``. With ``Type=forking`` systemd waits
for the declared PID file to appear before it considers the start finished, so it
waits for a file nothing ever creates, gives up after ``TimeoutStartSec`` (90s by
default) and marks the service **failed** — even though LiteSpeed itself started
perfectly and is serving traffic the whole time.

The visible consequences:

* ``systemctl restart lshttpd`` blocks for ~91s and then reports failure;
* the unit sits in ``failed``/``inactive`` while the server is up, so anything
  that trusts systemd (monitoring, ``restartsrv``, another restart attempt) sees a
  dead service and may start a *second* instance;
* the old and new instances then share the listening sockets via
  ``SO_REUSEPORT`` with different configurations, and the same URL answers
  inconsistently depending on which one accepts the connection.

Fix
---
Drop in ``/etc/systemd/system/lshttpd.service.d/cpanel-doctor-pidfile.conf``
overriding ``PIDFile`` with the path ``lswsctrl`` really writes, then
``systemctl daemon-reload``. The vendor unit is left untouched, so a LiteSpeed
package update that ships a corrected unit simply wins on its own terms and this
drop-in keeps agreeing with it. Restart time drops from ~91s to a few seconds and
systemd tracks the right process.

Note that LiteSpeed changes its main PID on every graceful restart, which
``Type=forking`` cannot follow; the unit can still drift out of sync after a
self-triggered reload. Re-syncing is ``systemctl reset-failed lshttpd &&
systemctl start lshttpd``, which adopts the PID from the (now correct) file.

Components
----------
1. ``pidfile_override``  systemd drop-in pointing PIDFile at lswsctrl's real path
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..core import Component, Patch, Runner

LSWSCTRL = "/usr/local/lsws/bin/lswsctrl"
UNIT = "lshttpd.service"
DROPIN_DIR = f"/etc/systemd/system/{UNIT}.d"
DROPIN = f"{DROPIN_DIR}/cpanel-doctor-pidfile.conf"
DEFAULT_PIDFILE = "/tmp/lshttpd/lshttpd.pid"

MARKER = "cpanel-doctor:lshttpd-pidfile"

_DROPIN_TEMPLATE = f"""# {MARKER}
# The packaged unit declares PIDFile=/var/run/lshttpd.pid, but lswsctrl writes the
# PID to %(pidfile)s. With Type=forking systemd waited for a file that never
# appeared, timed out after TimeoutStartSec (90s) and marked the service failed
# even though LiteSpeed had started correctly — leaving orphan instances behind.
[Service]
PIDFile=%(pidfile)s
"""


def _lswsctrl_pidfile(r: Runner) -> str:
    """The PID file path lswsctrl actually writes."""
    text = r.read(LSWSCTRL) or ""
    m = re.search(r"^PIDFILE=(\S+)", text, re.MULTILINE)
    return m.group(1) if m else DEFAULT_PIDFILE


def _unit_fragment_path(r: Runner) -> str:
    return (r.capture(["systemctl", "show", UNIT, "-p", "FragmentPath", "--value"]).stdout or "").strip()


def _vendor_pidfile(r: Runner) -> Optional[str]:
    """``PIDFile=`` as declared by the packaged unit itself (drop-ins excluded).

    Applicability is judged on the vendor file so that applying the patch — which
    changes only the *effective* value — does not make the patch read
    NOT_APPLICABLE afterwards.
    """
    path = _unit_fragment_path(r)
    if not path:
        return None
    text = r.read(path) or ""
    m = re.search(r"^\s*PIDFile\s*=\s*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def _effective_pidfile(r: Runner) -> str:
    return (r.capture(["systemctl", "show", UNIT, "-p", "PIDFile", "--value"]).stdout or "").strip()


class LshttpdPidfilePatch(Patch):
    id = "lshttpd-pidfile"
    name = "LiteSpeed systemd unit declares the wrong PID file"
    summary = (
        "Point lshttpd.service at the PID file lswsctrl really writes, so restarts "
        "stop timing out after 90s and leaving the unit failed."
    )
    description = (
        "LiteSpeed's lshttpd.service declares Type=forking with "
        "PIDFile=/var/run/lshttpd.pid, but lswsctrl writes the PID to "
        "/tmp/lshttpd/lshttpd.pid. systemd therefore waits for a file that is never "
        "created, times out after TimeoutStartSec (90s) and marks the service failed "
        "while LiteSpeed is in fact running and serving. The unit then no longer tracks "
        "the server, so a later start can spawn a second instance; both bind the same "
        "sockets via SO_REUSEPORT with different configurations and the same URL starts "
        "answering inconsistently. This patch adds a systemd drop-in with the correct "
        "PIDFile and reloads the daemon, leaving the vendor unit untouched."
    )
    caveats = (
        "Applies a drop-in under /etc/systemd/system and runs 'systemctl daemon-reload'; "
        "it does not restart LiteSpeed, so a unit already out of sync stays that way "
        "until 'systemctl reset-failed lshttpd && systemctl start lshttpd'. LiteSpeed "
        "changes its main PID on every graceful restart, which Type=forking cannot "
        "follow, so systemd can still drift out of sync after a self-triggered reload — "
        "this patch fixes the wrong path, not that limitation."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(LSWSCTRL):
            return False, "LiteSpeed not installed"
        if not _unit_fragment_path(r):
            return False, f"{UNIT} not found (LiteSpeed not managed by systemd?)"
        declared = _vendor_pidfile(r)
        if declared is None:
            return False, f"{UNIT} declares no PIDFile"
        real = _lswsctrl_pidfile(r)
        if declared == real:
            return False, f"unit already declares the correct PIDFile ({real})"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "pidfile_override",
                "systemd drop-in pointing PIDFile at the path lswsctrl writes",
                self._dropin_present,
                self._dropin_apply,
                self._dropin_remove,
            ),
        ]

    def _dropin_present(self, r: Runner) -> bool:
        if MARKER not in (r.read(DROPIN) or ""):
            return False
        # The drop-in only counts once systemd has actually loaded it.
        return _effective_pidfile(r) == _lswsctrl_pidfile(r)

    def _dropin_apply(self, r: Runner) -> None:
        r.write(DROPIN, _DROPIN_TEMPLATE % {"pidfile": _lswsctrl_pidfile(r)}, mode=0o644)
        r.run(["systemctl", "daemon-reload"])

    def _dropin_remove(self, r: Runner) -> None:
        if r.exists(DROPIN):
            r.remove(DROPIN)
            r.run(["systemctl", "daemon-reload"], check=False)

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"

        real = _lswsctrl_pidfile(r)
        effective = _effective_pidfile(r)
        if effective != real:
            return False, f"systemd still expects {effective or 'no PID file'}, lswsctrl writes {real}"

        # When the service is up, systemd should now be tracking the real process.
        active = (r.capture(["systemctl", "is-active", UNIT]).stdout or "").strip()
        if active != "active":
            return True, f"PIDFile now {real} (unit is {active or 'unknown'}, not verified live)"
        main_pid = (r.capture(["systemctl", "show", UNIT, "-p", "MainPID", "--value"]).stdout or "").strip()
        recorded = (r.read(real) or "").strip()
        if main_pid and recorded and main_pid != recorded:
            return False, f"systemd tracks PID {main_pid} but {real} records {recorded}"
        return True, f"PIDFile now {real}; systemd tracks the running server (PID {main_pid or '?'})"
