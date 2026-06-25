# Changelog

All notable changes to **cPanel Doctor** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — 2026-06-25
### Added
- Patch engine: component-based patches with applicable / not-applied / **drifted**
  / applied state detection, idempotent apply and clean reverse-removal.
- `pg-cpses` patch — fixes phpPgAdmin/PostgreSQL "Authentication failed" caused by
  a broken vendor `pam_cpses.so`, without weakening per-account isolation.
- Interactive **Textual** TUI and a scriptable CLI (`list`, `status`, `apply`,
  `remove`, `reapply`, `test`, `hook`), with `--dry-run`.
- Post-`upcp` self-heal hook (`hook install`) that re-applies drifted patches
  after cPanel updates.
