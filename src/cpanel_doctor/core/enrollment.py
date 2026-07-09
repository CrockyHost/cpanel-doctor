"""Persistent record of which patches the operator wants applied ("enrolled").

The post-upcp self-heal (`reapply`) needs to know the operator's *desired* state:
which patches should be on this host. Component-level drift detection alone can't
tell us that — a **single-component** patch that a cPanel update reverts wholesale
goes straight to ``NOT_APPLIED`` (never ``DRIFTED``), and blindly re-applying every
``NOT_APPLIED`` patch would apply things the operator never opted into.

So we record enrollment explicitly: `apply` enrolls a patch, `remove` un-enrolls
it, and `reapply` restores any enrolled patch that isn't fully ``APPLIED``. The
record lives under ``/var/cpanel`` (not ``/usr/local/cpanel``) precisely because
cPanel updates overwrite the latter but leave the former alone.
"""
from __future__ import annotations

import os
from typing import Iterable, Set

STATE_DIR = "/var/cpanel/cpanel_doctor"
ENROLLED_FILE = os.path.join(STATE_DIR, "enrolled")

_HEADER = (
    "# cpanel-doctor: patch ids the operator has applied, one per line.\n"
    "# Managed automatically by apply/remove; `reapply` restores these after a\n"
    "# cPanel update reverts them. Safe to hand-edit (blank lines / # comments ok).\n"
)


def enrolled_ids() -> Set[str]:
    """Return the set of enrolled patch ids (empty if nothing is enrolled yet)."""
    try:
        with open(ENROLLED_FILE, "r", encoding="utf-8") as fh:
            return {
                line.strip()
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            }
    except OSError:
        return set()


def _write(ids: Iterable[str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = ENROLLED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_HEADER)
        for pid in sorted(set(ids)):
            fh.write(pid + "\n")
    os.replace(tmp, ENROLLED_FILE)  # atomic


def enroll(patch_id: str) -> None:
    ids = enrolled_ids()
    if patch_id not in ids:
        ids.add(patch_id)
        _write(ids)


def unenroll(patch_id: str) -> None:
    ids = enrolled_ids()
    if patch_id in ids:
        ids.discard(patch_id)
        _write(ids)


def is_enrolled(patch_id: str) -> bool:
    return patch_id in enrolled_ids()
