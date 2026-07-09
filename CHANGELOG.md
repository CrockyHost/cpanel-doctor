# Changelog

All notable changes to **cPanel Doctor** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/).

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
