#!/bin/bash
# cpanel-doctor:lsws-suspend-reload
#
# Reload LiteSpeed after an account is suspended or unsuspended.
#
# cPanel regenerates /etc/apache2/conf.d/includes/account_suspensions.conf on every
# suspend/unsuspend, but it never touches /etc/apache2/conf/httpd.conf and never
# restarts the web server. LiteSpeed reads the Apache configuration only at
# start/restart and looks for changes on the *parent* config, not on the files it
# includes, so the new suspension block is never seen: the account shows as
# suspended in WHM while the site keeps being served, until LSWS happens to
# restart for some unrelated reason.
#
# MODE=graceful (default)
#   touch httpd.conf (so LSWS's auto-reload notices a change) + `lswsctrl restart`.
#   No downtime. The outgoing instance keeps draining and still answers with the
#   old configuration until gracefulRestartTimeout, so the suspension becomes
#   fully effective after roughly a minute rather than instantly.
#
# MODE=full
#   `systemctl restart lshttpd`. Effective immediately, at the cost of a few
#   seconds of hard downtime for *every* site on the server, on every suspend.
#   Pairs with the `lshttpd-pidfile` patch, without which that restart blocks for
#   TimeoutStartSec (90s) and leaves the unit "failed".
#
# Registered by cpanel-doctor on Whostmgr::Accounts::suspendacct and
# ::unsuspendacct (post stage). Managed file — edits are lost on reapply, except
# MODE, which is re-read from the installed copy.

MODE=graceful

HTTPD_CONF=/etc/apache2/conf/httpd.conf
LSWSCTRL=/usr/local/lsws/bin/lswsctrl
LOCK=/var/run/cpanel_doctor_lsws_suspend_reload.lock
LOG=/var/log/cpanel_doctor_lsws_suspend_reload.log

[ -x "$LSWSCTRL" ] || { echo "1 LiteSpeed not installed, nothing to reload"; exit 0; }

# Serialize reloads. Bulk suspensions would otherwise overlap restarts, and two
# LiteSpeed instances draining at once bind the same sockets (SO_REUSEPORT) with
# different configurations — the same URL then answers 200 or 302 at random.
(
    flock -w 120 9 || { echo "$(date '+%F %T') lock timeout" >>"$LOG"; exit 1; }
    touch "$HTTPD_CONF"
    if [ "$MODE" = "full" ]; then
        systemctl restart lshttpd >/dev/null 2>&1
    else
        "$LSWSCTRL" restart >/dev/null 2>&1
        sleep 5   # let the incoming instance take over before releasing the lock
    fi
    echo "$(date '+%F %T') reload (mode=$MODE)" >>"$LOG"
) 9>"$LOCK"

echo "1 LiteSpeed reloaded"
exit 0
