"""Patch: Force HTTPS Redirect greyed out (cPanel sees a past date).

Problem
-------
In *cPanel » Domains*, the **Force HTTPS Redirect** toggle is greyed out and
cannot be switched on, even though AutoSSL issues and renews valid certificates
normally. The toggle is enabled only when cPanel considers the domain's installed
certificate *currently valid*. That decision is made by ``_ssl_actually_valid()``
in ``Cpanel/Admin/Modules/Cpanel/ssl_call.pm`` (the ``GET_SSL_VHOSTS`` adminbin),
which compares "now" against the certificate's ``not_before`` / ``not_after``::

    my $time = time();
    return 0 if $time < iso2unix($cert->not_before());   # not yet valid
    return 0 if $time > iso2unix($cert->not_after());    # expired

When this process perceives a date in the *past*, a perfectly valid certificate
reads as "not yet valid", ``ssl_valid`` becomes 0, ``can_https_redirect`` becomes
false, and the UI disables the toggle. AutoSSL is unaffected because it does not
rely on this in-process clock.

Fix
---
Replace ``time()`` in that validity check with ``_real_now()``, which reads the
authoritative system clock from a clean child process (``/usr/bin/date -u +%s``
with ``LD_PRELOAD``/``FAKETIME`` cleared), bypassing whatever makes this process
see a past date. It falls back to ``time()`` if the clock can't be read. "now" is
computed once per ``GET_SSL_VHOSTS`` call and threaded into each check.

Components
----------
1. ``ssl_call``   the ``ssl_call.pm`` validity check, edited in place (orig backed up)

This is a single vendor file edited surgically; a cPanel update overwrites it,
which shows up as NOT-APPLIED and is healed by ``reapply`` / the post-upcp hook.
"""
from __future__ import annotations

import time as _time
from typing import List, Tuple

from ..core import Component, Patch, Runner, RunnerError

TARGET = "/usr/local/cpanel/Cpanel/Admin/Modules/Cpanel/ssl_call.pm"
CPANEL_PERL = "/usr/local/cpanel/3rdparty/bin/perl"

# The _real_now() helper we add to the module. Raw string: it contains Perl
# regex escapes (\A \z) that must survive verbatim.
_REAL_NOW_BLOCK = r"""# Return the authoritative current epoch time as reported by the system clock
# (the same value the `date` command shows). This deliberately reads the time
# from a clean child process, bypassing any process-level time override that
# can make this code perceive a date in the past -- which would otherwise treat
# a currently valid certificate as not-yet-valid/expired and grey out the
# "Force HTTPS Redirect" toggle in the Domains interface. Falls back to the
# in-process time() if the system clock cannot be read for any reason.
sub _real_now {
    local $ENV{'LD_PRELOAD'} = '';
    local $ENV{'FAKETIME'}   = '';
    my $epoch = `/usr/bin/date -u +%s 2>/dev/null`;
    chomp $epoch if defined $epoch;
    return ( defined $epoch && $epoch =~ m/\A[0-9]+\z/ ) ? ( $epoch + 0 ) : time();
}"""

# Ordered, reversible (old -> new) edits. apply() applies them top-down; remove()
# applies them new -> old bottom-up. Each `old` is matched against the pristine
# vendor file; each `new` is exactly what ends up on disk, so apply -> remove is
# byte-for-byte round-trippable.
_EDITS: List[Tuple[str, str]] = [
    # 1. Compute "now" once from the real clock, before the per-vhost loop.
    (
        "    my %vhost_hash;\n",
        "    my %vhost_hash;\n"
        "\n"
        "    # Determine \"now\" once from the authoritative system clock so that a past\n"
        "    # date perceived by this process cannot mark a currently valid certificate\n"
        "    # as invalid (which would disable the Force HTTPS Redirect control).\n"
        "    my $now = _real_now();\n",
    ),
    # 2. Thread $now into the vhost check.
    (
        "_ssl_actually_valid( $TLSIndex, $alias_map{$vh} )",
        "_ssl_actually_valid( $TLSIndex, $alias_map{$vh}, $now )",
    ),
    # 3. Thread $now into the alias check.
    (
        "_ssl_actually_valid( $TLSIndex, $alias_map{$alias} )",
        "_ssl_actually_valid( $TLSIndex, $alias_map{$alias}, $now )",
    ),
    # 4. Accept the passed-in $now.
    (
        "    my ( $TLSIndex, $dom_obj ) = @_;",
        "    my ( $TLSIndex, $dom_obj, $now ) = @_;",
    ),
    # 5. Use the authoritative now (fallback to _real_now if called directly).
    (
        "    my $time = time();",
        "    my $time = $now // _real_now();",
    ),
    # 6. Define _real_now() just before the module's trailing `1;`.
    (
        "    return 1;\n}\n\n1;\n",
        "    return 1;\n}\n\n" + _REAL_NOW_BLOCK + "\n\n1;\n",
    ),
]

MARKER = "sub _real_now"          # unmistakable "patch is present" marker
USES_REAL_NOW = "$now // _real_now()"


class HttpsRedirectDatePatch(Patch):
    id = "https-redirect-date"
    name = "Force HTTPS Redirect greyed out (past-date SSL validity)"
    summary = (
        "Make the Domains 'Force HTTPS Redirect' toggle usable when cPanel perceives a "
        "past date and wrongly treats valid certificates as not-yet-valid."
    )
    description = (
        "cPanel only enables the Force HTTPS Redirect toggle when it considers a domain's "
        "certificate currently valid. That check (_ssl_actually_valid in ssl_call.pm) "
        "compares the process clock against the cert's not_before/not_after. When this "
        "process sees a date in the past, valid certs read as 'not yet valid', so "
        "ssl_valid is false and the UI greys out the toggle (AutoSSL is unaffected). This "
        "patch makes that check read the authoritative system date (via /usr/bin/date in a "
        "clean child process), bypassing the bad in-process clock; it falls back to time() "
        "if the clock can't be read."
    )
    caveats = (
        "Edits a cPanel vendor file (ssl_call.pm) in place; the original is backed up to "
        "ssl_call.pm.orig. A cPanel update overwrites the file -> the patch reads as "
        "NOT APPLIED and is restored by 'reapply' or the post-upcp self-heal hook. The "
        "edit only affects how 'now' is read for the SSL-validity check; it does not "
        "change certificate trust or validation logic."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(TARGET):
            return False, "ssl_call.pm not found (not a cPanel host?)"
        text = r.read(TARGET) or ""
        if MARKER in text:
            return True, ""  # already applied
        if "sub _ssl_actually_valid" not in text or "    my $time = time();" not in text:
            return False, "this cPanel build's ssl_call.pm does not match the expected code"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "ssl_call",
                "ssl_call.pm SSL-validity check reads the real system date",
                self._present,
                self._apply,
                self._remove,
            ),
        ]

    def _present(self, r: Runner) -> bool:
        text = r.read(TARGET) or ""
        return MARKER in text and USES_REAL_NOW in text

    def _apply(self, r: Runner) -> None:
        text = r.read(TARGET)
        if text is None:
            raise RunnerError(f"cannot read {TARGET}")
        for old, new in _EDITS:
            if old not in text:
                raise RunnerError(
                    f"expected anchor not found in ssl_call.pm; refusing to patch a "
                    f"changed build (missing: {old.strip()[:60]!r})"
                )
            text = text.replace(old, new, 1)
        r.backup(TARGET)                    # -> ssl_call.pm.orig (once, never clobbered)
        r.write(TARGET, text, mode=0o755)   # vendor file is executable (0755)

    def _remove(self, r: Runner) -> None:
        text = r.read(TARGET)
        if text is None:
            return
        if MARKER not in text:
            return  # nothing of ours present
        for old, new in reversed(_EDITS):
            text = text.replace(new, old, 1)
        r.write(TARGET, text, mode=0o755)

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        """Confirm the file compiles and the check now sources the real date."""
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"

        text = r.read(TARGET) or ""
        if MARKER not in text or USES_REAL_NOW not in text:
            return False, "patch markers missing from ssl_call.pm"

        # 1. The patched module still compiles under cPanel's bundled perl.
        if r.exists(CPANEL_PERL):
            res = r.capture([CPANEL_PERL, "-c", TARGET], timeout=60)
            blob = ((res.stdout or "") + (res.stderr or "")).strip()
            if "syntax OK" not in blob:
                return False, f"perl -c failed: {blob[:200]}"

        # 2. The clock source the patch uses returns the real wall-clock date.
        res = r.capture(["/usr/bin/date", "-u", "+%s"])
        epoch = (res.stdout or "").strip()
        if not epoch.isdigit():
            return False, "could not read the system clock via /usr/bin/date"
        skew = abs(int(epoch) - int(_time.time()))
        if skew > 5:
            return False, f"system-clock source disagrees with the process clock by {skew}s"
        human = _time.strftime("%Y-%m-%d %H:%M:%SZ", _time.gmtime(int(epoch)))
        return True, f"compiles; SSL-validity check now reads the real date ({human})"
