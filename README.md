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

### `account-startdate` — new accounts get a past creation date
When an account is created in WHM, its creation date (`STARTDATE` in
`/var/cpanel/users/<user>`, shown as `unix_startdate` by `whmapi1 listaccts`) is
written with a timestamp in the **past**, even though the host's OS clock is
correct — so every freshly created account shows a wrong creation date in WHM.
The patch installs a `Whostmgr::Accounts::Create` **post** hook that, right after
creation, rewrites `STARTDATE` to the **authoritative system date** (`/usr/bin/date`
in a clean child process, bypassing the bad in-process clock) via
`Cpanel::Config::CpUserGuard`, updating both the datastore and the `users.cache`
entry. It only corrects a date that is clearly wrong (off by more than a day) and
always reports success, so it can never block account creation.

Components `hook_script` (the Perl hook on disk) and `hook_registration` (the
`manage_hooks` entry). It fixes accounts created **after** it is applied; it does
not rewrite pre-existing dates. Neither component is a vendor file, but if either
goes missing it reads as **DRIFTED** and is healed by `reapply` / the post-upcp hook.

### `https-redirect-date` — Force HTTPS Redirect greyed out
The **Force HTTPS Redirect** toggle in *cPanel » Domains* is greyed out even
though AutoSSL issues valid certificates. cPanel only enables it when it judges
the certificate *currently valid*; that check (`_ssl_actually_valid` in
`Cpanel/Admin/Modules/Cpanel/ssl_call.pm`) compares the **process clock** against
the cert's `not_before`/`not_after`. When this process perceives a date in the
**past**, valid certs read as "not yet valid", `ssl_valid` is false, and the UI
disables the toggle (AutoSSL is unaffected). The patch makes that check read the
**authoritative system date** (`/usr/bin/date` in a clean child process), falling
back to `time()` if the clock can't be read.

Single component `ssl_call`: the vendor file is edited in place (original backed
up to `ssl_call.pm.orig`). A cPanel update overwrites it → the patch reads as
**NOT APPLIED** and is restored by `reapply` / the post-upcp hook.

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
