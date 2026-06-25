"""Patch: phpPgAdmin / PostgreSQL cpses authentication.

Problem
-------
On affected cPanel & WHM builds the vendor PAM module ``pam_cpses.so`` rejects
every valid cpses session for PostgreSQL. The cPanel session is created fine, but
``pam_cpses`` denies it, so phpPgAdmin shows *"Authentication failed"* for every
account, server-wide. MySQL/phpMyAdmin are unaffected.

Fix
---
Route the ``postgresql_cpses`` PAM service through our own validator, which
accepts a login only when the supplied credential matches a recent, genuine cpses
session secret for that user. ``pg_hba``'s ``samerole`` rule is left untouched, so
per-account database isolation is preserved.

Components
----------
1. ``validator``   the auth script in /usr/local/cpanel/bin
2. ``pam``         /etc/pam.d/postgresql_cpses -> our validator (orig backed up)
3. ``loopback``    systemd unit binding 127.0.0.200 (the cpses PAM IP) on lo
4. ``phppgadmin``  point phpPgAdmin at 127.0.0.200 (this is what an update resets)
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple

from ..core import Component, Patch, Runner, asset_text

VALIDATOR_PATH = "/usr/local/cpanel/bin/pam_cpses_postgres.sh"
PAM_SERVICE_PATH = "/etc/pam.d/postgresql_cpses"
SERVICE_NAME = "cpses-postgres-ip.service"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
CPSES_IP = "127.0.0.200"

# phpPgAdmin ships these (cPanel-managed; reset by updates)
PPA_ROOT = "/usr/local/cpanel/3rdparty/share/phpPgAdmin"
PPA_CONFIG = f"{PPA_ROOT}/conf/config.inc.php"
PPA_INTRO = f"{PPA_ROOT}/intro.php"
SOCKET = "/var/run/postgresql"

PAM_CONTENT = (
    "#%PAM-1.0\n"
    "# Managed by cpanel-doctor (patch: pg-cpses). Original: postgresql_cpses.orig\n"
    "# The stock pam_cpses.so rejects valid cpses sessions on this build; validate\n"
    "# via our own checker (exact-match against the recent session secret).\n"
    f"auth     required  pam_exec.so expose_authtok quiet {VALIDATOR_PATH}\n"
    "account  required  pam_permit.so\n"
)
STOCK_PAM_CONTENT = (
    "#%PAM-1.0\n"
    "auth    required pam_cpses.so\n"
    "account required pam_cpses.so\n"
)


def _host_line_value(text: str) -> str:
    m = re.search(r"\$conf\['servers'\]\[0\]\['host'\]\s*=\s*'([^']*)'", text)
    return m.group(1) if m else ""


class PgCpsesPatch(Patch):
    id = "pg-cpses"
    name = "phpPgAdmin / PostgreSQL cpses login"
    summary = "Fix 'Authentication failed' in phpPgAdmin caused by a broken pam_cpses.so."
    description = (
        "On affected cPanel builds the vendor pam_cpses.so module rejects every valid "
        "PostgreSQL cpses session, so phpPgAdmin fails to log in for all accounts. This "
        "patch routes the postgresql_cpses PAM service through a validator that accepts "
        "genuine, recent cpses sessions only, and points phpPgAdmin at the cpses PAM IP "
        f"({CPSES_IP}). MySQL/phpMyAdmin are unaffected."
    )
    caveats = (
        "Replaces the vendor PAM module for the postgresql_cpses service only. Account "
        "isolation is preserved (pg_hba 'samerole' is untouched); the path is loopback-"
        "only; the session secret is unreadable by ordinary users. A cPanel update will "
        "reset the phpPgAdmin 'phppgadmin' component -> install the post-upcp hook to "
        "self-heal."
    )

    # -- applicability ------------------------------------------------------
    def applicable(self, runner: Runner) -> Tuple[bool, str]:
        if not runner.exists(PPA_ROOT):
            return False, "phpPgAdmin is not installed"
        if not runner.exists("/usr/bin/psql"):
            return False, "PostgreSQL is not installed"
        return True, ""

    # -- components ---------------------------------------------------------
    def components(self) -> List[Component]:
        return [
            Component("validator", "cpses auth validator script",
                      self._validator_present, self._validator_apply, self._validator_remove),
            Component("pam", "postgresql_cpses PAM service -> validator",
                      self._pam_present, self._pam_apply, self._pam_remove),
            Component("loopback", f"{CPSES_IP} bound on lo (persistent service)",
                      self._loopback_present, self._loopback_apply, self._loopback_remove),
            Component("phppgadmin", f"phpPgAdmin pointed at {CPSES_IP}",
                      self._ppa_present, self._ppa_apply, self._ppa_remove),
        ]

    # -- 1. validator -------------------------------------------------------
    def _validator_present(self, r: Runner) -> bool:
        text = r.read(VALIDATOR_PATH)
        return bool(text) and "cpses_pg" in text and os.access(VALIDATOR_PATH, os.X_OK)

    def _validator_apply(self, r: Runner) -> None:
        r.write(VALIDATOR_PATH, asset_text("pam_cpses_postgres.sh"), mode=0o755)

    def _validator_remove(self, r: Runner) -> None:
        r.remove(VALIDATOR_PATH)

    # -- 2. pam service -----------------------------------------------------
    def _pam_present(self, r: Runner) -> bool:
        text = r.read(PAM_SERVICE_PATH) or ""
        return "pam_cpses_postgres.sh" in text

    def _pam_apply(self, r: Runner) -> None:
        if r.exists(PAM_SERVICE_PATH):
            r.backup(PAM_SERVICE_PATH)               # -> postgresql_cpses.orig
        r.write(PAM_SERVICE_PATH, PAM_CONTENT, mode=0o644)

    def _pam_remove(self, r: Runner) -> None:
        if r.exists(PAM_SERVICE_PATH + ".orig"):
            r.restore(PAM_SERVICE_PATH)
        else:
            r.write(PAM_SERVICE_PATH, STOCK_PAM_CONTENT, mode=0o644)

    # -- 3. loopback service ------------------------------------------------
    def _service_enabled(self, r: Runner) -> bool:
        return r.capture(["systemctl", "is-enabled", SERVICE_NAME]).stdout.strip() == "enabled"

    def _ip_bound(self, r: Runner) -> bool:
        return CPSES_IP in (r.capture(["ip", "addr", "show", "lo"]).stdout or "")

    def _loopback_present(self, r: Runner) -> bool:
        return r.exists(SERVICE_PATH) and self._service_enabled(r) and self._ip_bound(r)

    def _loopback_apply(self, r: Runner) -> None:
        r.write(SERVICE_PATH, asset_text("cpses-postgres-ip.service"), mode=0o644)
        r.run(["systemctl", "daemon-reload"])
        r.run(["systemctl", "enable", "--now", SERVICE_NAME])

    def _loopback_remove(self, r: Runner) -> None:
        if r.exists(SERVICE_PATH):
            r.run(["systemctl", "disable", "--now", SERVICE_NAME], check=False)
            r.remove(SERVICE_PATH)
            r.run(["systemctl", "daemon-reload"], check=False)
        r.run(["ip", "addr", "del", f"{CPSES_IP}/32", "dev", "lo"], check=False)

    # -- 4. phpPgAdmin config ----------------------------------------------
    def _ppa_present(self, r: Runner) -> bool:
        cfg = r.read(PPA_CONFIG) or ""
        intro = r.read(PPA_INTRO) or ""
        return _host_line_value(cfg) == CPSES_IP and f"server={CPSES_IP}:5432:allow" in intro

    def _ppa_apply(self, r: Runner) -> None:
        cfg = r.read(PPA_CONFIG)
        if cfg is not None:
            cfg = re.sub(
                r"(\$conf\['servers'\]\[0\]\['host'\]\s*=\s*')[^']*(';)",
                rf"\g<1>{CPSES_IP}\g<2>", cfg, count=1,
            )
            r.write(PPA_CONFIG, cfg, mode=0o644)
        intro = r.read(PPA_INTRO)
        if intro is not None:
            intro = intro.replace(f"server={SOCKET}:5432:allow", f"server={CPSES_IP}:5432:allow")
            r.write(PPA_INTRO, intro, mode=0o644)

    def _ppa_remove(self, r: Runner) -> None:
        cfg = r.read(PPA_CONFIG)
        if cfg is not None:
            cfg = re.sub(
                r"(\$conf\['servers'\]\[0\]\['host'\]\s*=\s*')[^']*(';)",
                rf"\g<1>{SOCKET}\g<2>", cfg, count=1,
            )
            r.write(PPA_CONFIG, cfg, mode=0o644)
        intro = r.read(PPA_INTRO)
        if intro is not None:
            intro = intro.replace(f"server={CPSES_IP}:5432:allow", f"server={SOCKET}:5432:allow")
            r.write(PPA_INTRO, intro, mode=0o644)

    # -- functional self-test ----------------------------------------------
    def self_test(self, r: Runner) -> Tuple[bool, str]:
        """Confirm a source-127.0.0.200 connection reaches the PAM rule (cleartext)."""
        ok, _ = self.applicable(r)
        if not ok:
            return True, "not applicable"
        if not self._ip_bound(r):
            return False, f"{CPSES_IP} is not bound on lo"
        probe = (
            "import socket,struct\n"
            f"s=socket.socket();s.settimeout(5);s.connect(('{CPSES_IP}',5432))\n"
            "b=struct.pack('!I',196608)+b'user\\x00postgres\\x00database\\x00postgres\\x00\\x00'\n"
            "s.sendall(struct.pack('!I',len(b)+4)+b)\n"
            "print('R' if s.recv(1)==b'R' else 'X')\n"
        )
        res = r.capture(["python3", "-c", probe])
        if "R" in (res.stdout or ""):
            return True, f"PAM path on {CPSES_IP} reachable"
        return False, f"could not reach PostgreSQL on {CPSES_IP}: {res.stdout}{res.stderr}".strip()
