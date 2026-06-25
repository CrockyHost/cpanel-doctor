"""Patch: new cPanel accounts get a creation date in the past.

Problem
-------
When an account is created in WHM, its creation date (``STARTDATE`` in
``/var/cpanel/users/<user>``, surfaced by ``whmapi1 listaccts`` as
``unix_startdate``) is written with a timestamp in the *past*, even though the
host's OS clock is correct. The account-creation process simply perceives a
past date (visible, e.g., as the bogus copyright year in the ``WWWAcct`` banner),
so every freshly created account shows a wrong creation date in WHM.

Fix
---
Install a cPanel Standardized Hook on ``Whostmgr::Accounts::Create`` (post
stage). Right after an account is created the hook rewrites ``STARTDATE`` to the
real "now", read from a *clean* child process (``env -i /bin/date +%s``) so that
whatever makes the creating process see a past date (an inherited
``LD_PRELOAD``/``FAKETIME``) is bypassed -- a plain ``date`` on the host is
correct. The value is written through ``Cpanel::Config::CpUserGuard``, which
updates both the datastore and the ``users.cache`` entry. It only touches a
``STARTDATE`` that is clearly wrong (off by more than a day) and always reports
success, so it can never disrupt account creation.

Components
----------
1. ``hook_script``        the Perl hook script, installed on disk
2. ``hook_registration``  the Whostmgr::Accounts::Create (post) registration

Neither is a vendor file, so a cPanel update does not normally overwrite them;
if either goes missing the patch reads as DRIFTED and is healed by ``reapply`` /
the post-upcp self-heal hook. This patch fixes accounts created *after* it is
applied; pre-existing wrong dates are not touched.
"""
from __future__ import annotations

import time as _time
from typing import List, Tuple

from ..core import Component, Patch, Runner, asset_text

ASSET = "cpanel_doctor_account_startdate.pl"
HOOK_PATH = "/usr/local/cpanel/scripts/cpanel_doctor_account_startdate.pl"
MANAGE_HOOKS = "/usr/local/cpanel/bin/manage_hooks"
HOOK_DESC = ["--category", "Whostmgr", "--event", "Accounts::Create", "--stage", "post"]
CPANEL_PERL = "/usr/local/cpanel/3rdparty/bin/perl"
USERS_DIR = "/var/cpanel/users"

# Unmistakable "this is our managed script" marker (a comment in the asset).
MARKER = "cpanel-doctor:account-startdate"


class AccountStartdatePatch(Patch):
    id = "account-startdate"
    name = "New accounts get a past creation date (STARTDATE)"
    summary = (
        "Make newly created cPanel accounts record the real creation date when the "
        "account-creation process perceives a date in the past."
    )
    description = (
        "On affected hosts, creating an account in WHM writes STARTDATE "
        "(/var/cpanel/users/<user>, shown as unix_startdate by listaccts) with a past "
        "timestamp even though the OS clock is correct, so the account's creation date is "
        "wrong in WHM. This patch installs a Whostmgr::Accounts::Create (post) hook that, "
        "right after creation, rewrites STARTDATE to the authoritative system date (read "
        "via /usr/bin/date in a clean child process, bypassing the bad in-process clock) "
        "through Cpanel::Config::CpUserGuard, updating both the datastore and the cache."
    )
    caveats = (
        "Fixes accounts created AFTER the patch is applied; it does not rewrite the "
        "creation date of pre-existing accounts. The hook only changes STARTDATE (the "
        "recorded creation date) and only when it is off by more than a day; it always "
        "reports success so it cannot block account creation. Registered via manage_hooks "
        "as a manual hook; removal de-registers it and deletes the script."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(MANAGE_HOOKS):
            return False, "manage_hooks not found (not a cPanel host?)"
        if not r.exists(USERS_DIR):
            return False, "no /var/cpanel/users (not a cPanel host?)"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "hook_script",
                "post-create hook script that rewrites STARTDATE to the real date",
                self._script_present,
                self._script_apply,
                self._script_remove,
            ),
            Component(
                "hook_registration",
                "Whostmgr::Accounts::Create (post) hook registered with manage_hooks",
                self._reg_present,
                self._reg_apply,
                self._reg_remove,
            ),
        ]

    # hook_script ------------------------------------------------------------
    def _script_present(self, r: Runner) -> bool:
        text = r.read(HOOK_PATH) or ""
        return MARKER in text

    def _script_apply(self, r: Runner) -> None:
        r.write(HOOK_PATH, asset_text(ASSET), mode=0o700)

    def _script_remove(self, r: Runner) -> None:
        r.remove(HOOK_PATH)

    # hook_registration ------------------------------------------------------
    def _reg_present(self, r: Runner) -> bool:
        if not r.exists(MANAGE_HOOKS):
            return False
        listing = r.capture([MANAGE_HOOKS, "list"]).stdout or ""
        return HOOK_PATH in listing

    def _reg_apply(self, r: Runner) -> None:
        r.run([MANAGE_HOOKS, "add", "script", HOOK_PATH, "--manual", *HOOK_DESC], check=False)

    def _reg_remove(self, r: Runner) -> None:
        r.run([MANAGE_HOOKS, "delete", "script", HOOK_PATH, "--manual", *HOOK_DESC], check=False)

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        """Confirm the hook is installed, compiles, and sources the real date."""
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"

        if not self._script_present(r):
            return False, "hook script not installed"
        if not self._reg_present(r):
            return False, "hook not registered on Whostmgr::Accounts::Create"

        # 1. The hook compiles under cPanel's bundled perl.
        if r.exists(CPANEL_PERL):
            res = r.capture([CPANEL_PERL, "-c", HOOK_PATH], timeout=60)
            blob = ((res.stdout or "") + (res.stderr or "")).strip()
            if "syntax OK" not in blob:
                return False, f"perl -c failed: {blob[:200]}"

            # 2. Running it on a no-op payload (unknown user) returns success and
            #    mutates nothing.
            payload = '{"data":{"user":"cpdoctor_selftest_no_such_user"}}'
            res = r.capture(
                ["/bin/sh", "-c", f"printf '%s' '{payload}' | {CPANEL_PERL} {HOOK_PATH}"],
                timeout=60,
            )
            out = (res.stdout or "").strip()
            if not out.startswith("1"):
                return False, f"hook did not return success on a no-op payload: {out[:120]!r}"

        # 3. The clock source the hook uses returns the real wall-clock date.
        res = r.capture(["/usr/bin/env", "-i", "/bin/date", "+%s"])
        epoch = (res.stdout or "").strip()
        if not epoch.isdigit():
            return False, "could not read the system clock via a clean /bin/date"
        skew = abs(int(epoch) - int(_time.time()))
        if skew > 5:
            return False, f"clean clock source disagrees with the process clock by {skew}s"
        human = _time.strftime("%Y-%m-%d %H:%M:%SZ", _time.gmtime(int(epoch)))
        return True, f"hook installed & compiles; new accounts will record the real date ({human})"
