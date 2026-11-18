"""Patch: cPanel records every login with a wrong (past, often frozen) date.

Problem
-------
cPanel appends each login to ``~/.lastlogin`` as ``<ip> # <YYYY-MM-DD HH:MM:SS +ZZZZ>``
(``Cpanel::LastLogin::Tiny``). The timestamp is taken from the cPanel process
clock. On affected hosts that process perceives a date in the past, often one
frozen instant, so every login since then carries the same wrong date, both in
the file and in Team Manager, which reads it back. The host's OS clock is
correct.

``Cpanel::LastLogin::Tiny`` is compiled into the ``cpanel`` and ``cpsrvd``
binaries, so editing the ``.pm`` file on disk would change nothing.

Fix
---
The kernel sets the file's modification time from the real clock, so the mtime
of ``.lastlogin`` is the true time of the login cPanel just appended as the last
line. A small script, run every minute by a systemd timer, copies that mtime into
the last line's timestamp when the two disagree by more than two minutes, then
restores the mtime. Earlier lines are never rewritten. On a host where cPanel's
clock is right the two always agree and the script writes nothing.

Each file is handled with the effective uid/gid of the account that owns it,
opened with ``O_NOFOLLOW``, and skipped unless it is a small regular file owned
by that account, so a customer cannot point the root-run timer at another file.

Components
----------
1. ``fixer_script``  the Perl script, installed on disk
2. ``timer``         the systemd service + timer that run it every minute

Neither is a vendor file, so a cPanel update does not normally overwrite them;
if either goes missing the patch reads as DRIFTED and is healed by ``reapply`` /
the post-upcp self-heal hook.
"""
from __future__ import annotations

from typing import List, Tuple

from ..core import Component, Patch, Runner, asset_text

SCRIPT_ASSET = "cpanel_doctor_lastlogin_date.pl"
SCRIPT_PATH = "/usr/local/cpanel/scripts/cpanel_doctor_lastlogin_date.pl"
SERVICE_NAME = "cpanel-doctor-lastlogin-date.service"
TIMER_NAME = "cpanel-doctor-lastlogin-date.timer"
UNIT_DIR = "/etc/systemd/system"
SERVICE_PATH = f"{UNIT_DIR}/{SERVICE_NAME}"
TIMER_PATH = f"{UNIT_DIR}/{TIMER_NAME}"
CPANEL_PERL = "/usr/local/cpanel/3rdparty/bin/perl"
USERS_DIR = "/var/cpanel/users"
# The vendor module whose on-disk format the script parses. Used only to confirm
# this cPanel build still writes "<ip> # <timestamp>" lines.
TINY_PM = "/usr/local/cpanel/Cpanel/LastLogin/Tiny.pm"
LINE_FORMAT = '"$currenthost # $timestamp"'

# Unmistakable "this is our managed file" marker (a comment in every asset).
MARKER = "cpanel-doctor:lastlogin-date"


class LastloginDatePatch(Patch):
    id = "lastlogin-date"
    name = "Logins recorded with a wrong date (~/.lastlogin)"
    summary = (
        "Record the real date of each cPanel login when the cPanel process perceives a "
        "date in the past and stamps every login with it."
    )
    description = (
        "cPanel writes each login to ~/.lastlogin as '<ip> # <timestamp>', taking the "
        "timestamp from its own process clock. On affected hosts that clock is stuck in the "
        "past, so every login shows the same wrong date (also in Team Manager), while the "
        "OS clock is right. The writer is compiled into the cpanel/cpsrvd binaries and "
        "cannot be edited, so this patch installs a script, run every minute by a systemd "
        "timer, that copies the file's kernel-set modification time (the real time of the "
        "last login) into the last line's timestamp whenever the two disagree."
    )
    caveats = (
        "A new login shows the wrong date for up to a minute, until the timer runs. Only "
        "the newest line of each file is corrected: logins already pushed down the list "
        "keep the date they were written with. Each file is handled as its owner (seteuid, "
        "O_NOFOLLOW, regular files only). Removal disables the timer and deletes the units "
        "and the script; corrected dates stay as they are."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(USERS_DIR):
            return False, "no /var/cpanel/users (not a cPanel host?)"
        if not r.exists(TINY_PM):
            return False, "Cpanel/LastLogin/Tiny.pm not found (not a cPanel host?)"
        if LINE_FORMAT not in (r.read(TINY_PM) or ""):
            return False, "this cPanel build no longer writes '<ip> # <timestamp>' lines"
        if not r.exists("/usr/bin/systemctl") and not r.exists("/bin/systemctl"):
            return False, "systemd not found"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "fixer_script",
                "script that copies the real login time into ~/.lastlogin",
                self._script_present,
                self._script_apply,
                self._script_remove,
            ),
            Component(
                "timer",
                f"{TIMER_NAME} runs the script every minute",
                self._timer_present,
                self._timer_apply,
                self._timer_remove,
            ),
        ]

    # fixer_script -----------------------------------------------------------
    def _script_present(self, r: Runner) -> bool:
        return MARKER in (r.read(SCRIPT_PATH) or "")

    def _script_apply(self, r: Runner) -> None:
        r.write(SCRIPT_PATH, asset_text(SCRIPT_ASSET), mode=0o700)

    def _script_remove(self, r: Runner) -> None:
        r.remove(SCRIPT_PATH)

    # timer ------------------------------------------------------------------
    def _timer_active(self, r: Runner) -> bool:
        enabled = r.capture(["systemctl", "is-enabled", TIMER_NAME]).stdout.strip()
        active = r.capture(["systemctl", "is-active", TIMER_NAME]).stdout.strip()
        return enabled == "enabled" and active == "active"

    def _timer_present(self, r: Runner) -> bool:
        return (
            MARKER in (r.read(SERVICE_PATH) or "")
            and MARKER in (r.read(TIMER_PATH) or "")
            and self._timer_active(r)
        )

    def _timer_apply(self, r: Runner) -> None:
        r.write(SERVICE_PATH, asset_text(SERVICE_NAME), mode=0o644)
        r.write(TIMER_PATH, asset_text(TIMER_NAME), mode=0o644)
        r.run(["systemctl", "daemon-reload"])
        r.run(["systemctl", "enable", "--now", TIMER_NAME])

    def _timer_remove(self, r: Runner) -> None:
        if r.exists(TIMER_PATH):
            r.run(["systemctl", "disable", "--now", TIMER_NAME], check=False)
            r.remove(TIMER_PATH)
        if r.exists(SERVICE_PATH):
            r.remove(SERVICE_PATH)
        r.run(["systemctl", "daemon-reload"], check=False)

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        """Confirm the script compiles, the timer runs, and no login date is left wrong."""
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"
        if not self._script_present(r):
            return False, "fixer script not installed"
        if not self._timer_present(r):
            return False, f"{TIMER_NAME} not enabled and active"

        perl = CPANEL_PERL if r.exists(CPANEL_PERL) else "perl"
        res = r.capture([perl, "-c", SCRIPT_PATH], timeout=60)
        blob = ((res.stdout or "") + (res.stderr or "")).strip()
        if "syntax OK" not in blob:
            return False, f"perl -c failed: {blob[:200]}"

        # Run it now (so a fresh apply is effective at once), then check that
        # nothing is left to correct.
        r.run([perl, SCRIPT_PATH], check=False)
        res = r.capture([perl, SCRIPT_PATH, "--check"], timeout=120)
        out = (res.stdout or "").strip()
        if "would-fix" in out:
            return False, f"login dates still wrong after a run: {out.splitlines()[-1]}"
        summary = out.splitlines()[-1] if out else "no output"
        return True, f"timer active; every last login carries its real date ({summary})"
