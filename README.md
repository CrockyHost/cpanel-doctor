# cPanel Doctor

**Diagnose and fix known cPanel/WHM problems — intelligently, interactively, reversibly.**

cPanel Doctor models each fix as a **patch** made of independent **components**. It
*knows* whether a patch is applicable, fully applied, not applied, or **drifted**
(partly reset — e.g. by a cPanel update), and it can apply, re-apply only the
drifted bits, or cleanly remove a patch. There's a colourful [Textual](https://textual.textualize.io/)
TUI and a scriptable CLI.

```
┌ cPanel Doctor v0.1.0 ──────────────────────────────────────────────┐
│ ID         Status        Patch                                     │
│ pg-cpses   APPLIED       phpPgAdmin / PostgreSQL cpses login        │
│ ...                                                                  │
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

### `pg-cpses` — phpPgAdmin / PostgreSQL cpses login
On affected builds the vendor `pam_cpses.so` rejects **every** valid PostgreSQL
cpses session, so phpPgAdmin shows *"Authentication failed"* for all accounts
(MySQL/phpMyAdmin are fine). The patch routes the `postgresql_cpses` PAM service
through a validator that accepts only **recent, genuine** cpses session secrets,
and points phpPgAdmin at the cpses PAM IP (`127.0.0.200`).

**Security:** `pg_hba`'s `samerole` is left untouched (per-account DB isolation is
preserved); the path is loopback-only; the 32-char session secret lives in
`/var/cpanel/cpses/keys` (`root:cpses 0750`) and is unreadable by ordinary users,
so it can't be forged. Components: `validator`, `pam` (backs up the original),
`loopback`, `phppgadmin`.

## How a patch works (extending)

Add a `Patch` subclass under `cpanel_doctor/patches/`; it's auto-discovered.
A patch is a list of `Component`s, each with `present` / `apply` / `remove`. The
doctor derives status from the components, so drift detection and surgical
re-apply come for free.

```python
class MyPatch(Patch):
    id = "my-fix"
    name = "Short title"
    def components(self):
        return [Component("thing", "what it is", present, apply, remove)]
```

## Safety

- `--dry-run` previews every action without touching the system.
- `remove` reverses each component (restoring `.orig` backups where taken).
- Read-only `status`/`list`/`test` never change anything and don't need root.

## License

MIT © crocky.host
