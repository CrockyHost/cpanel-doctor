# Patches

Every fix in cPanel Doctor is a **patch** built from independent **components**
(a file, a service, a config edit, a hook …). The doctor derives each patch's
status from its components, so *applicable / not-applied / **drifted** / applied*
detection, idempotent apply, surgical re-apply and clean removal all come for free.

| ID | Fixes |
|----|-------|
| [`account-startdate`](#account-startdate--new-accounts-get-a-past-creation-date) | new accounts recorded with a creation date in the past |
| [`https-redirect-date`](#https-redirect-date--force-https-redirect-greyed-out) | greyed-out **Force HTTPS Redirect** toggle |
| [`pdns-upcp-removal`](#pdns-upcp-removal--powerdns-uninstalled-by-a-cpanel-update) | PowerDNS uninstalled by a cPanel update, DNS down |
| [`pg-cpses`](#pg-cpses--phppgadmin--postgresql-cpses-login) | phpPgAdmin *"Authentication failed"* (broken `pam_cpses.so`) |

---

## `account-startdate` — new accounts get a past creation date
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

## `https-redirect-date` — Force HTTPS Redirect greyed out
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

## `pdns-upcp-removal` — PowerDNS uninstalled by a cPanel update
On a host whose authoritative nameserver is PowerDNS
(`local_nameserver_type=powerdns`), a cPanel update (`/scripts/upcp`) can decide
the `cpanel-pdns` package is *unneeded* and remove it mid-run — the upcp log shows
`Uninstalling unneeded rpms: cpanel-pdns` and `pdns.conf saved as
pdns.conf.rpmsave`. The package, the `/usr/sbin/pdns_server` binary and the
`pdns.service` unit all vanish, nothing listens on port 53, and **every** domain
on the box stops resolving. `chkservd` can't recover it — it restarts a stopped
service, it does not reinstall a deleted package — so the outage lasts until
someone reinstalls PowerDNS by hand (seen after major bumps like `…cp130`).

The patch reinstalls and reconfigures PowerDNS with the supported command
`setupnameserver --force powerdns` (which reinstalls `cpanel-pdns`, regenerates
`/etc/pdns/pdns.conf`, re-enables/starts the service and restores monitoring;
`/var/named` zone files are untouched). The trick is *when* it runs: it is modelled
so that a package removal reads as **DRIFTED**, and cPanel Doctor's existing
post-upcp self-heal hook (which runs `reapply`, healing drifted patches) fires it
automatically after the very update that removed PowerDNS.

Components `guard` (a marker under `/var/lib/cpanel-doctor` that survives upcp —
its persistence is what makes a removal read as DRIFTED rather than NOT-APPLIED,
so `reapply` picks it up) and `pdns_pkg` (present only while armed **and**
`cpanel-pdns` installed; healed via `setupnameserver --force`). `applicable()`
only returns true on hosts already set to PowerDNS, so it never converts a
bind/nsd/disabled host; and `pdns_pkg`'s removal is a deliberate no-op, so
removing the patch merely disarms the guard and **never** uninstalls DNS.

## `pg-cpses` — phpPgAdmin / PostgreSQL cpses login
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

---

## Writing a new patch
Drop a `Patch` subclass under `cpanel_doctor/patches/`; it's auto-discovered. A
patch is a list of `Component`s, each with `present` / `apply` / `remove`. Because
status is derived from the components, drift detection and surgical re-apply are
automatic.

```python
from cpanel_doctor.core import Component, Patch, Runner

class MyPatch(Patch):
    id = "my-fix"
    name = "Short title"
    summary = "One line shown in `list`."
    description = "Longer text shown in the TUI detail pane."

    def applicable(self, r: Runner):
        return r.exists("/some/marker"), "why not, if not"

    def components(self):
        return [
            Component(
                key="thing",
                description="what it is",
                present=lambda r: r.exists("/etc/thing"),
                apply=lambda r: r.write("/etc/thing", "...", 0o644),
                remove=lambda r: r.remove("/etc/thing"),
            ),
        ]

    def self_test(self, r: Runner):     # optional functional check after apply
        return True, "ok"
```

Guidelines:

* **Idempotent** — `present` must be cheap and side-effect free; `apply` only
  touches what's missing.
* **Reversible** — `remove` undoes `apply`; back up vendor files (`Runner.backup`)
  so removal can restore them.
* **Honest applicability** — return `NOT_APPLICABLE` rather than editing a file you
  don't recognise (see `https-redirect-date`, which refuses an unfamiliar build).
* **Never break the host** — a hook/patch should fail safe (always report success
  to cPanel where relevant) and prefer correcting only clearly-wrong state.
