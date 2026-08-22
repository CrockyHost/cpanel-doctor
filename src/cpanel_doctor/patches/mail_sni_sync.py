"""Patch: mail services present the server hostname cert instead of the domain's.

Problem
-------
When a domain has a valid CA-issued SSL certificate installed for its website,
cPanel is supposed to also wire that certificate into the mail services through
**Mail SNI**: it copies the cert to ``/var/cpanel/ssl/domain_tls/<domain>/`` and
adds a ``local_name`` block to ``/etc/dovecot/sni.conf`` so that Dovecot (IMAP/POP)
and Exim (SMTP) present the *domain's* certificate when a client connects to
``mail.<domain>`` with TLS.

Occasionally a domain's cert is issued/installed (typically by AutoSSL) but the
mail SNI entry is **not** (re)built for it — observed on this host after an
unusual restore path (account created with a self-signed placeholder, real
AutoSSL cert installed later). The symptom is nasty and hard to diagnose from the
client side::

    $ openssl s_client -connect mail.<domain>:993 -servername mail.<domain>
    subject=CN = <server-hostname>          # <-- WRONG: the box's own cert
    issuer=C = US, O = Let's Encrypt, ...

Because the presented certificate name (the server hostname) does not match
``mail.<domain>``, every mail client that validates certificates (Outlook, Apple
Mail, Thunderbird, phones) refuses the **secure** IMAP/SMTP ports (993/465/587
STARTTLS) with a certificate-name-mismatch error. Users conclude "SSL doesn't
work" and fall back to (or are auto-configured onto) plaintext — or cannot
connect at all. The IMAP/SMTP services themselves are fine; only the certificate
handed out for the domain is wrong.

Fix
---
Rebuild the Dovecot mail SNI include from the installed certificates and restart
the mail services with the officially supported command::

    /usr/local/cpanel/scripts/build_mail_sni --rebuild_dovecot_sni_conf --restartsrvs

That regenerates ``/etc/dovecot/sni.conf`` (and the per-domain ``domain_tls``
copies) so every domain that owns a real certificate presents it on the mail
ports, then reloads Dovecot and Exim. It is idempotent — on an already-synced
host it simply rewrites the same content.

Model
-----
A single component, ``sni_synced``, reports **present** only while *every*
cPanel-managed domain that has a CA-issued web certificate also appears in
``/etc/dovecot/sni.conf``. Freshly created accounts that still carry only a
self-signed placeholder are intentionally ignored (cPanel does not give those a
mail SNI entry either), and the server's own hostname certificate — the global
default Dovecot/Exim cert, which needs no ``local_name`` — is excluded, so a
healthy host reads cleanly **APPLIED** rather than perpetually drifting.

Applying the patch enrolls it, so the post-upcp self-heal hook's ``reapply`` will
rebuild mail SNI again if a later cPanel update (or a certificate renewal that
leaves a domain out) desyncs it. Removing the patch is a no-op beyond
un-enrolling: taking a correct certificate back off the mail ports would only
re-break secure mail, so it is never done.

Components
----------
1. ``sni_synced``  every CA-secured managed domain is present in the Dovecot mail
                   SNI; healed with ``build_mail_sni --rebuild_dovecot_sni_conf``.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Set, Tuple

from ..core import Component, Patch, Runner, RunnerError

BUILD_MAIL_SNI = "/usr/local/cpanel/scripts/build_mail_sni"
SNI_CONF = "/etc/dovecot/sni.conf"
APACHE_TLS_DIR = "/var/cpanel/ssl/apache_tls"
USERDATADOMAINS = "/etc/userdatadomains"
# cPanel builds mail SNI for a customer's primary-ish domains — not for arbitrary
# web subdomains (type "sub"), which are expected to use the parent's mail host.
_MAIL_SNI_TYPES = {"main", "addon", "parked"}


def _sni_localnames(r: Runner) -> Set[str]:
    """The ``local_name`` values in dovecot's SNI config, verbatim and lower-cased
    (wildcards like ``*.example.com`` are kept as-is for correct coverage tests)."""
    text = r.read(SNI_CONF) or ""
    return {name.strip().lower() for name in re.findall(r'local_name\s+"([^"]+)"', text) if name.strip()}


def _covered_by_sni(domain: str, localnames: Set[str]) -> bool:
    """Whether ``domain`` presents its own cert via an existing SNI block.

    True if the domain itself is a ``local_name``, or if the wildcard one level up
    (``*.parent``) is — cPanel emits a ``*.example.com`` block that covers
    ``mail.example.com`` and friends.
    """
    if domain in localnames:
        return True
    parent = domain.split(".", 1)[1] if "." in domain else ""
    return bool(parent) and f"*.{parent}" in localnames


def _sni_candidate_domains(r: Runner) -> Dict[str, str]:
    """Map each mail-SNI candidate domain to the vhost whose certificate serves it.

    Candidates are the main/addon/parked entries of ``/etc/userdatadomains``
    (user-created ``sub`` domains are excluded). The value is the account's main
    domain, because that is the name cPanel files the certificate under: a parked
    domain like ``oxidev.ro`` has no ``apache_tls/oxidev.ro`` of its own and is
    served by ``apache_tls/oxidev-systems.ro``, whose SAN list covers it.
    """
    text = r.read(USERDATADOMAINS) or ""
    doms: Dict[str, str] = {}
    for line in text.splitlines():
        domain, sep, rest = line.partition(":")
        if not sep:
            continue
        # Fields are '=='-separated (user==owner==type==maindomain==docroot==...).
        # Find the type token by value so we don't depend on its exact position.
        raw = [f.strip() for f in rest.split("==")]
        fields = [f.lower() for f in raw]
        dtype = next((f for f in fields if f in {"main", "addon", "parked", "sub"}), "")
        if dtype not in _MAIL_SNI_TYPES:
            continue
        idx = fields.index(dtype)
        vhost = fields[idx + 1] if idx + 1 < len(fields) else ""
        name = domain.strip().lower()
        doms[name] = vhost or name
    return doms


def _is_ca_signed(r: Runner, cert_path: str) -> bool:
    """True if the leaf cert is CA-issued (issuer != subject), i.e. not self-signed."""
    try:
        res = r.capture(["openssl", "x509", "-in", cert_path, "-noout", "-subject", "-issuer"])
    except (OSError, ValueError):
        return False
    subject = issuer = ""
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "subject":
            subject = value.strip()
        elif key == "issuer":
            issuer = value.strip()
    return bool(issuer) and bool(subject) and issuer != subject


def _cert_names(r: Runner, cert_path: str) -> Set[str]:
    """Every DNS name the certificate is valid for (CN plus subjectAltName)."""
    try:
        res = r.capture(["openssl", "x509", "-in", cert_path, "-noout", "-ext", "subjectAltName"])
    except (OSError, ValueError):
        return set()
    return {m.strip().lower() for m in re.findall(r"DNS:([^,\s]+)", res.stdout or "")}


def _name_covered(domain: str, names: Set[str]) -> bool:
    """Whether ``domain`` is covered exactly or by a one-level wildcard."""
    if domain in names:
        return True
    parent = domain.split(".", 1)[1] if "." in domain else ""
    return bool(parent) and f"*.{parent}" in names


def _domains_missing_from_sni(r: Runner) -> List[str]:
    """main/addon/parked domains that own a real (CA) web cert but present the
    wrong certificate on the mail ports because they are absent from mail SNI."""
    localnames = _sni_localnames(r)
    missing: List[str] = []
    for domain, vhost in sorted(_sni_candidate_domains(r).items()):
        if _covered_by_sni(domain, localnames):
            continue  # already presents its own cert on the mail ports
        # The certificate is filed under the vhost, not the domain: a parked or
        # addon domain has no apache_tls directory of its own. Looking it up by
        # domain silently skipped exactly those, so a parked domain could sit on
        # the wrong mail certificate while this reported everything in order.
        combined = os.path.join(APACHE_TLS_DIR, vhost, "combined")
        # Only a CA-issued web cert *should* have a mail SNI entry; a self-signed
        # placeholder legitimately does not, so it is not counted as drift.
        if not (r.exists(combined) and _is_ca_signed(r, combined)):
            continue
        # ...and it must actually be valid for this name, or wiring it into SNI
        # would hand out a certificate that fails hostname verification anyway.
        if _name_covered(domain, _cert_names(r, combined)):
            missing.append(domain)
    return missing


class MailSniSyncPatch(Patch):
    id = "mail-sni-sync"
    name = "Mail services present the wrong (hostname) TLS certificate"
    summary = (
        "Rebuild Dovecot/Exim mail SNI so mail.<domain> presents the domain's own "
        "certificate instead of the server hostname cert (fixes secure IMAP/SMTP)."
    )
    description = (
        "When a domain has a valid CA-issued SSL certificate but its Mail SNI entry was "
        "not (re)built, Dovecot and Exim keep presenting the server's hostname certificate "
        "on mail.<domain>. Mail clients that validate certificates then reject the secure "
        "IMAP/POP/SMTP ports (993/995/465/587) with a name mismatch and fall back to "
        "plaintext or fail to connect entirely. This patch detects any cPanel-managed "
        "domain that owns a real certificate yet is missing from /etc/dovecot/sni.conf and "
        "heals it by running 'build_mail_sni --rebuild_dovecot_sni_conf --restartsrvs', "
        "which rewrites the SNI include and restarts the mail services. Once applied it is "
        "enrolled, so the post-upcp self-heal hook re-syncs mail SNI after updates or "
        "certificate renewals that desync it."
    )
    caveats = (
        "Healing runs 'build_mail_sni --rebuild_dovecot_sni_conf --restartsrvs', which "
        "restarts Dovecot and Exim (a brief mail blip). It only rebuilds SNI from the "
        "certificates cPanel already has; it never issues certificates. Removing the patch "
        "only stops the self-heal — it never strips a certificate back off the mail ports."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, r: Runner) -> Tuple[bool, str]:
        if not r.exists(BUILD_MAIL_SNI):
            return False, "build_mail_sni not found (not a cPanel host?)"
        if not r.exists(os.path.dirname(SNI_CONF)):
            return False, "/etc/dovecot not found (Dovecot not installed?)"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component(
                "sni_synced",
                "every CA-secured managed domain presents its own cert on the mail ports",
                self._synced_present,
                self._synced_apply,
                self._synced_remove,
            ),
        ]

    def _synced_present(self, r: Runner) -> bool:
        return not _domains_missing_from_sni(r)

    def _synced_apply(self, r: Runner) -> None:
        r.run(
            [BUILD_MAIL_SNI, "--rebuild_dovecot_sni_conf", "--restartsrvs"],
            timeout=300,
        )
        if not r.dry_run:
            missing = _domains_missing_from_sni(r)
            if missing:
                raise RunnerError(
                    "build_mail_sni ran but these domains are still missing from mail "
                    "SNI: " + ", ".join(missing)
                )

    def _synced_remove(self, r: Runner) -> None:
        # Deliberately a no-op: removing a correct certificate from the mail ports
        # would only re-break secure mail. Removing the patch just stops self-heal.
        r.note("sni_synced: leaving mail SNI in place (remove only stops self-heal)")

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        applicable, why = self.applicable(r)
        if not applicable:
            return True, f"not applicable: {why}"
        missing = _domains_missing_from_sni(r)
        if missing:
            shown = ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")
            return False, f"{len(missing)} domain(s) missing from mail SNI: {shown}"
        return True, "all CA-secured managed domains present in mail SNI"
