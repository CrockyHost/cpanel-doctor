# Patches

Every fix in cPanel Doctor is a **patch** built from independent **components**
(a file, a service, a config edit, a hook …). The doctor derives each patch's
status from its components, so *applicable / not-applied / **drifted** / applied*
detection, idempotent apply, surgical re-apply and clean removal all come for free.

| ID | Fixes |
|----|-------|
| [`account-startdate`](#account-startdate--new-accounts-get-a-past-creation-date) | new accounts recorded with a creation date in the past |
| [`https-redirect-date`](#https-redirect-date--force-https-redirect-greyed-out) | greyed-out **Force HTTPS Redirect** toggle |
| [`lshttpd-pidfile`](#lshttpd-pidfile--litespeed-systemd-unit-points-at-the-wrong-pid-file) | `systemctl restart lshttpd` timing out after 90s and leaving the unit failed |
| [`lsws-ssl-reload`](#lsws-ssl-reload--new-certificates-are-not-served-until-litespeed-restarts) | a newly issued certificate not served, the domain answered with the server hostname's cert |
| [`lsws-suspend-reload`](#lsws-suspend-reload--suspended-accounts-still-served-by-litespeed) | a suspended account whose site keeps serving normally |
| [`mail-sni-sync`](#mail-sni-sync--mail-ports-present-the-wrong-hostname-certificate) | secure IMAP/SMTP presenting the server hostname cert on `mail.<domain>` |
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

## `lshttpd-pidfile` — LiteSpeed systemd unit points at the wrong PID file
LiteSpeed ships `lshttpd.service` with `Type=forking` and
`PIDFile=/var/run/lshttpd.pid`, but `lswsctrl` writes the PID to
`/tmp/lshttpd/lshttpd.pid`. systemd therefore waits for a file nothing ever
creates, gives up after `TimeoutStartSec` (90s) and marks the service **failed** —
while LiteSpeed is running and serving traffic the entire time.

The knock-on effects are worse than the cosmetics: the unit no longer tracks the
server, so anything trusting systemd (monitoring, `restartsrv`, a second restart
attempt) sees a dead service and can start a **second** instance. Both then bind
the same sockets via `SO_REUSEPORT` with different configurations, and the same
URL answers 200 or 302 depending on which instance accepts the connection — which
looks exactly like a caching bug and is not one.

The patch drops in `cpanel-doctor-pidfile.conf` under
`/etc/systemd/system/lshttpd.service.d/` overriding `PIDFile` with the path read
out of `lswsctrl` itself, then runs `daemon-reload`. Restart time falls from ~91s
to a few seconds. The vendor unit is left untouched, so a LiteSpeed update that
corrects it simply agrees. Single component `pidfile_override`, which counts as
present only once `systemctl show` reports the new value — a drop-in on disk that
`daemon-reload` has not picked up is not a fix in effect. `applicable()` reads the
**vendor** unit rather than the effective value, so the patch stays APPLIED after
being applied instead of flipping to NOT-APPLICABLE.

**Caveat:** LiteSpeed changes its main PID on every graceful restart, which
`Type=forking` cannot follow, so systemd can still drift out of sync after a
self-triggered reload; re-sync with
`systemctl reset-failed lshttpd && systemctl start lshttpd`.

## `lsws-ssl-reload` — new certificates are not served until LiteSpeed restarts
Installing a certificate — by hand in WHM, or automatically at the end of an
AutoSSL run — does everything right on the cPanel side: the certificate lands in
`/var/cpanel/ssl/apache_tls/<domain>/` and `/etc/apache2/conf/httpd.conf` is
regenerated with the matching `<VirtualHost ...:443>` block. **Nothing restarts the
web server.**

LiteSpeed reads the Apache configuration, and the certificates it points at, only
at start/restart. A server process older than the certificate keeps serving the
previous one — and a vhost that had *no* certificate before is not in the running
configuration at all, so the connection falls through to the default listener and
is answered with the **server hostname's** certificate:

```
$ openssl s_client -connect <ip>:443 -servername newsite.example
subject=CN = cpcalendars.<server-hostname>
```

Browsers reject that outright; if the hostname certificate is itself expired the
error becomes a confusing mix of "wrong name" and "expired". The signature is a
LiteSpeed process older than the certificate:

```
# ps -o lstart= -p $(cat /tmp/lshttpd/lshttpd.pid)   # Sat Aug 22 19:10:20
# stat -c %y /var/cpanel/ssl/apache_tls/newsite.example/combined
                                                     2026-08-22 19:27:24
```

Certificates installed *before* that process started are served correctly, so
domains secured weeks apart behave differently and the fault looks intermittent.

The patch installs `Whostmgr::AutoSSL::installssl` / `Whostmgr::SSL::installssl`
(post) hooks that touch `httpd.conf` and reload LiteSpeed. Components:
`hook_script`, `hook_autossl`, `hook_manual` — one per registration, so losing a
single event reads as DRIFTED and is healed by `reapply`. The self-test compares
the running server's start time against the newest certificate under
`apache_tls/`, naming the domain that is still being served the wrong one.

**Coalescing.** AutoSSL fires its event once *per vhost*, so a naive hook would
ask for one restart per domain on every run. The installed script takes its lock
**non-blockingly** and exits when another invocation already holds it; that holder
sleeps `DEBOUNCE` (20s) before reloading, absorbing the rest of the burst while
still reloading after the last certificate was written. A full AutoSSL run
collapses into a single restart. The worker is detached, so the hook never delays
the AutoSSL run or the WHM API response.

**Caveat:** defaults to `MODE=graceful` — no downtime, but the outgoing instance
drains with the old certificate, so a new one is fully live after about a minute.
`MODE=full` makes the switch immediate via `systemctl restart lshttpd` at the cost
of a few seconds of downtime for **every** site per install; apply
[`lshttpd-pidfile`](#lshttpd-pidfile--litespeed-systemd-unit-points-at-the-wrong-pid-file)
first or that restart blocks for 90s. Either mode still waits out `DEBOUNCE`, so a
certificate goes live seconds after issuance rather than the same instant.

## `lsws-suspend-reload` — suspended accounts still served by LiteSpeed
Suspending an account does everything right on the cPanel side — it writes
`/var/cpanel/suspended/<user>`, sets `SUSPENDED=1` and regenerates
`/etc/apache2/conf.d/includes/account_suspensions.conf` with the redirect block to
`suspendedpage.cgi`. On a LiteSpeed host that block is **never read**: LSWS parses
the Apache config only at start/restart, and watches the *parent* `httpd.conf`
rather than the files it `Include`s. Suspending rewrites only the include, so
nothing tells LiteSpeed to re-read anything and **the site stays online while WHM
reports the account suspended** — potentially for days.

The signature is a LiteSpeed process older than the suspensions file:

```
# ps -o lstart= -p $(cat /tmp/lshttpd/lshttpd.pid)   # Thu Aug 20 13:47:14
# stat -c %y /etc/apache2/conf.d/includes/account_suspensions.conf
                                                     2026-08-22 13:00:32
```

Accounts suspended *before* that process started redirect correctly — their blocks
were in the file when it parsed the config — so two accounts suspended weeks apart
behave differently and the bug looks random.

The patch installs a `Whostmgr::Accounts::suspendacct` / `::unsuspendacct` (post)
hook that touches `httpd.conf` and reloads LiteSpeed. Components: `hook_script`,
`hook_suspend`, `hook_unsuspend` — one per registration, so losing a single event
reads as DRIFTED and is healed by `reapply`. The self-test goes beyond "is it
installed" and compares the running server's start time against the suspensions
file, reporting failure when the server is already behind.

**Caveat:** the hook defaults to `MODE=graceful` — no downtime, but the outgoing
LiteSpeed instance keeps draining and answering with the old configuration, so a
suspension lands after roughly a minute rather than instantly. `MODE=full` in the
installed script makes it immediate via `systemctl restart lshttpd`, at the cost
of a few seconds of downtime for **every** site on the server per suspend; apply
[`lshttpd-pidfile`](#lshttpd-pidfile--litespeed-systemd-unit-points-at-the-wrong-pid-file)
first or that restart blocks for 90s. Either way the hook holds an `flock`, so
bulk suspensions cannot stack overlapping restarts and leave two instances
serving different configurations at once.

## `mail-sni-sync` — mail ports present the wrong (hostname) certificate
When a domain has a valid CA certificate for its **website**, cPanel is supposed
to wire that same certificate into the mail services via **Mail SNI** — copying it
to `/var/cpanel/ssl/domain_tls/<domain>/` and adding a `local_name` block to
`/etc/dovecot/sni.conf` so Dovecot (IMAP/POP) and Exim (SMTP) hand out the
*domain's* cert when a client connects to `mail.<domain>` over TLS. Occasionally
the cert is installed (typically by AutoSSL) but the SNI entry is not rebuilt for
it, so the mail ports keep presenting the **server hostname** certificate:

```
$ openssl s_client -connect mail.example.com:993 -servername mail.example.com | openssl x509 -noout -subject
subject=CN = server-hostname.example-host.net     # wrong: not mail.example.com
```

Because the presented name doesn't match `mail.example.com`, certificate-validating
clients (Outlook, Apple Mail, Thunderbird, phones) refuse the secure IMAP/POP/SMTP
ports (993 / 995 / 465 / 587) with a name-mismatch error and fall back to plaintext
or fail to connect. The services themselves are fine — only the certificate handed
out for the domain is wrong.

Single component `sni_synced`: reports **present** only while every domain cPanel
itself would give mail SNI, and that owns a *CA-issued* web certificate naming it,
also appears in `/etc/dovecot/sni.conf`. The rule mirrors cPanel's own builder
(`Cpanel::AdvConfig::dovecotSNI`): an account's **main** domain always qualifies,
an **addon/parked** domain only while it has at least one email account
(`~<user>/etc/<domain>/passwd`). Web-only subdomains (type `sub`), mailbox-less
addons and self-signed placeholders are excluded. cPanel deliberately gives those
no mail SNI, and `build_mail_sni` can never add them, so a healthy host reads
cleanly **APPLIED** instead of failing its heal on every post-upcp run. Healing runs
`build_mail_sni --rebuild_dovecot_sni_conf --restartsrvs`, which regenerates the
SNI include and restarts Dovecot/Exim. Applying enrolls the patch, so the
post-upcp hook's `reapply` re-syncs mail SNI after an update or a certificate
renewal that leaves a domain out. Removal only stops the self-heal — it never
strips a certificate back off the mail ports.

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
