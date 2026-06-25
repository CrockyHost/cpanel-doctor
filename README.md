# cPanel Doctor

**Diagnose and fix known cPanel/WHM problems — intelligently, interactively, reversibly.**

cPanel Doctor models each fix as a **patch** made of independent **components**. It
*knows* whether a patch is applicable, fully applied, not applied, or **drifted**
(partly reset — e.g. by a cPanel update), and it can apply, re-apply only the
drifted bits, or cleanly remove a patch. There's a colourful [Textual](https://textual.textualize.io/)
TUI and a scriptable CLI.

```
┌ cPanel Doctor v0.2.0 ──────────────────────────────────────────────┐
│ ID                   Status     Patch                              │
│ account-startdate    APPLIED    New accounts' creation date (past)  │
│ https-redirect-date  APPLIED    Force HTTPS Redirect (past date)    │
│ pg-cpses             APPLIED    phpPgAdmin / PostgreSQL cpses login │
│ post-upcp self-heal hook: installed (System::upcp post)            │
└─────────────────────────────────────────────────────────────────────┘
```

## Install

```bash
pipx install cpanel-doctor        # recommended
# or
pip install cpanel-doctor
```

Requires **Python 3.8+** (cPanel's system Python may be older — use `pipx`,
a venv, or your distro's newer Python). Most actions modify system files and
need **root**.

## Use

```bash
cpanel-doctor                 # interactive TUI (default)
cpanel-doctor list            # one-line status per patch
cpanel-doctor status pg-cpses # detailed, per-component status
sudo cpanel-doctor apply pg-cpses
sudo cpanel-doctor apply pg-cpses --dry-run   # preview, change nothing
sudo cpanel-doctor remove pg-cpses
cpanel-doctor test pg-cpses    # functional self-test
sudo cpanel-doctor hook install # self-heal after cPanel updates
```

### TUI keys
`a` apply · `r` remove · `h` re-apply drift · `t` self-test · `k` toggle hook ·
`d` refresh · `q` quit.

## Self-healing after `upcp`

cPanel updates overwrite vendor-managed files (for `pg-cpses`, phpPgAdmin's
`config.inc.php`/`intro.php`), which **drifts** a patch. Install the hook once:

```bash
sudo cpanel-doctor hook install
```

It registers a cPanel Standardized Hook on `System::upcp` (post stage) that runs
`cpanel-doctor reapply` after every update, healing only the drifted components.

## Patches

Three patches ship today — see **[PATCHES.md](PATCHES.md)** for full descriptions,
security notes and a guide to writing your own.

| ID | Fixes |
|----|-------|
| `account-startdate` | new accounts recorded with a creation date in the past |
| `https-redirect-date` | greyed-out **Force HTTPS Redirect** toggle |
| `pg-cpses` | phpPgAdmin *"Authentication failed"* (broken `pam_cpses.so`) |

New patches are auto-discovered — drop a `Patch` subclass in
`cpanel_doctor/patches/` (see [PATCHES.md](PATCHES.md#writing-a-new-patch)).

## Safety

- `--dry-run` previews every action without touching the system.
- `remove` reverses each component (restoring `.orig` backups where taken).
- Read-only `status`/`list`/`test` never change anything and don't need root.

## License

MIT © crocky.host
