"""Post-upcp self-healing hook management.

cPanel updates overwrite vendor-managed files, silently undoing the ``phppgadmin``
component of some patches. We register a cPanel Standardized Hook on
``System::upcp`` (post stage) that runs ``cpanel-doctor reapply`` after every
update, so drifted patches heal themselves.
"""
from __future__ import annotations

import os
from typing import Tuple

from .core import Runner, asset_text

HOOK_PATH = "/usr/local/cpanel/scripts/cpanel_doctor_posthook.sh"
MANAGE_HOOKS = "/usr/local/cpanel/bin/manage_hooks"
HOOK_DESC = ["--category", "System", "--event", "upcp", "--stage", "post"]


def available(runner: Runner) -> bool:
    """Whether this looks like a cPanel host with the hook manager."""
    return runner.exists(MANAGE_HOOKS)


def installed(runner: Runner) -> bool:
    if not runner.exists(HOOK_PATH):
        return False
    listing = runner.capture([MANAGE_HOOKS, "list"]).stdout or ""
    return HOOK_PATH in listing


def install(runner: Runner) -> None:
    runner.write(HOOK_PATH, asset_text("cpanel_doctor_posthook.sh"), mode=0o755)
    if available(runner):
        runner.run([MANAGE_HOOKS, "add", "script", HOOK_PATH, "--manual", *HOOK_DESC], check=False)


def remove(runner: Runner) -> None:
    if available(runner):
        runner.run([MANAGE_HOOKS, "delete", "script", HOOK_PATH, "--manual", *HOOK_DESC], check=False)
    if runner.exists(HOOK_PATH):
        runner.remove(HOOK_PATH)


def status(runner: Runner) -> Tuple[bool, str]:
    if not available(runner):
        return False, "manage_hooks not found (not a cPanel host?)"
    if installed(runner):
        return True, "installed (System::upcp post)"
    return False, "not installed"
