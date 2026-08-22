# Changelog

All notable changes to **cPanel Doctor** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.5.0] — 2026-08-22
### Added
- `lsws-suspend-reload` patch — fixes **suspended accounts whose sites keep being
  served**. Suspending an account regenerates
  `/etc/apache2/conf.d/includes/account_suspensions.conf`, but LiteSpeed parses the
  Apache configuration only at start/restart and watches the parent `httpd.conf`
  rather than the files it includes, so nothing ever makes it read the new redirect
  block: WHM reports the account suspended while the site stays online, sometimes for
  days. Accounts suspended before the running LSWS process started behave correctly,
  which makes the bug look random. The patch installs a
  `Whostmgr::Accounts::suspendacct` / `::unsuspendacct` (post) hook that touches
  `httpd.conf` and reloads LiteSpeed, serialized with an `flock` so bulk suspensions
  cannot stack overlapping restarts. Its self-test compares the running server's
  start time against the suspensions file, so a host that is already behind is
  reported rather than assumed healthy.
- `lshttpd-pidfile` patch — fixes **`lshttpd.service` declaring a PID file that is
  never written**. The packaged unit pairs `Type=forking` with
  `PIDFile=/var/run/lshttpd.pid` while `lswsctrl` writes `/tmp/lshttpd/lshttpd.pid`,
  so systemd waits for a file nothing creates, times out after `TimeoutStartSec`
  (90s) and marks the service failed even though LiteSpeed started correctly. The
  unit then stops tracking the server, so a later start can spawn a second instance;
  both bind the same sockets via `SO_REUSEPORT` with different configurations and the
  same URL begins answering inconsistently. A systemd drop-in supplies the real path
  (read out of `lswsctrl` itself), cutting restart time from ~91s to a few seconds
  and leaving the vendor unit untouched.

## [0.4.0] — 2026-07-28
### Added
- `mail-sni-sync` patch — fixes **mail services presenting the server hostname
  certificate instead of the domain's**. When a domain has a valid CA certificate
  for its website but its Mail SNI entry was never (re)built, Dovecot and Exim keep
  serving the box's hostname cert on `mail.<domain>`, so certificate-validating
  clients (Outlook, Apple Mail, phones) reject the secure IMAP/POP/SMTP ports
  (993/995/465/587) with a name mismatch and fall back to plaintext or fail to
  connect. The patch detects any main/addon/parked domain that owns a real
  certificate yet is missing from `/etc/dovecot/sni.conf` — ignoring web-only
  subdomains and self-signed placeholders, which cPanel intentionally leaves out —
  and heals it with `build_mail_sni --rebuild_dovecot_sni_conf --restartsrvs`.
  Enrolled on apply, so the post-upcp self-heal hook re-syncs mail SNI after
  updates or certificate renewals that desync it.

## [0.3.1] — 2026-07-09
### Fixed
- **Self-heal now restores single-file patches that a cPanel update reverts.**
  `reapply` healed only `DRIFTED` patches; a single-component patch (e.g.
  `https-redirect-date`) wiped wholesale by an update goes to `NOT_APPLIED`, which
  was silently skipped, so it stayed reverted until re-applied by hand.
- **A crash in one patch no longer aborts the whole `reapply` run.** Post-upcp
  hooks run with a minimal PATH where `ip`/`systemctl` aren't found; `pg-cpses`'s
  state check raised `FileNotFoundError` while `reapply` merely *evaluated* patch
  state (outside any try/except), killing every self-heal run. Each patch is now
  fully isolated, and `Runner` hardens the subprocess PATH (adds the sbin dirs) so
  system tools resolve regardless of the caller's environment. The post-upcp hook
  also exports a full PATH.
- Enrollment tracking (`/var/cpanel/cpanel_doctor/enrolled`): `apply` enrolls a
  patch, `remove` un-enrolls it, and `reapply` restores enrolled-but-reverted
  patches only — so self-heal never applies patches the operator didn't opt into.

## [0.3.0] — 2026-07-09
### Added
- `pdns-upcp-removal` patch — fixes **PowerDNS being uninstalled by a cPanel
  update**. On a PowerDNS host (`local_nameserver_type=powerdns`), `upcp` can
  classify `cpanel-pdns` as "unneeded" and remove it mid-run (*"Uninstalling
  unneeded rpms: cpanel-pdns"*), deleting the package, the `pdns_server` binary
  and the `pdns.service` unit — nothing then listens on port 53 and every domain
  on the box stops resolving, with `chkservd` unable to help (the package is
  gone, not just stopped). The patch arms a persistent guard so such a removal
  reads as **DRIFTED**, and the post-upcp self-heal hook reinstalls PowerDNS via
  `setupnameserver --force powerdns` (regenerating the config, restarting the
  service and restoring monitoring; zone files in `/var/named` are untouched).
  Components `guard` and `pdns_pkg`; only arms on hosts already set to PowerDNS
  and never uninstalls DNS on removal.

## [0.2.0] — 2026-06-26
### Added
- `account-startdate` patch — fixes newly created cPanel accounts being recorded
  with a creation date in the **past** (`STARTDATE` in `/var/cpanel/users/<user>`,
  surfaced as `unix_startdate`) even though the OS clock is correct. Installs a
  `Whostmgr::Accounts::Create` (post) hook that rewrites `STARTDATE` to the
  authoritative system date (read via `/usr/bin/date` in a clean child process)
  through `Cpanel::Config::CpUserGuard`, updating both the datastore and the
  `users.cache`. Components `hook_script` and `hook_registration`; self-heals via
  `reapply` / the post-upcp hook if either is dropped.

## [0.1.0] — 2026-06-25
### Added
- Patch engine: component-based patches with applicable / not-applied / **drifted**
  / applied state detection, idempotent apply and clean reverse-removal.
- `pg-cpses` patch — fixes phpPgAdmin/PostgreSQL "Authentication failed" caused by
  a broken vendor `pam_cpses.so`, without weakening per-account isolation.
- `https-redirect-date` patch — fixes the greyed-out **Force HTTPS Redirect** toggle
  in cPanel » Domains when the SSL-validity check (`ssl_call.pm`) perceives a past
  date and treats valid certificates as not-yet-valid; the check is made to read the
  authoritative system date instead.
- Interactive **Textual** TUI and a scriptable CLI (`list`, `status`, `apply`,
  `remove`, `reapply`, `test`, `hook`), with `--dry-run`.
- Post-`upcp` self-heal hook (`hook install`) that re-applies drifted patches
  after cPanel updates.
