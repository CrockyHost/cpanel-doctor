#!/bin/bash
# cpanel-doctor:lsws-ssl-reload
#
# Reload LiteSpeed after a TLS certificate is installed.
#
# cPanel writes the new certificate under /var/cpanel/ssl/apache_tls/<domain>/ and
# regenerates /etc/apache2/conf/httpd.conf, but it never restarts the web server.
# LiteSpeed reads the Apache configuration -- and the certificates it points at --
# only at start/restart, so a server process older than the certificate keeps
# serving the previous one. A vhost that had no certificate at all falls through
# to the default listener and answers with the *hostname* certificate, which is
# why a freshly secured domain shows a certificate issued for another name
# entirely (typically cpanel.<server-hostname> or the first SAN on it).
#
# Registered by cpanel-doctor on Whostmgr::AutoSSL::installssl and
# Whostmgr::SSL::installssl (post stage).
#
# Coalescing
#   AutoSSL fires the event once per vhost, so a full run would otherwise ask for
#   one restart per domain. This script takes the lock non-blockingly and exits
#   when another invocation already holds it: that holder sleeps DEBOUNCE seconds
#   before reloading, so it absorbs the rest of the burst and its single reload
#   still happens after the last certificate was written. A whole AutoSSL run
#   collapses into one restart.
#
# The work is detached, so the hook returns immediately and never delays the
# AutoSSL run or the WHM "Install an SSL Certificate" response.
#
# MODE=graceful (default)
#   touch httpd.conf (so LSWS's change detection notices) + `lswsctrl restart`.
#   No downtime; the outgoing instance drains with the old certificate for up to
#   gracefulRestartTimeout, so the new one is fully live after about a minute.
#
# MODE=full
#   `systemctl restart lshttpd`. Live immediately, at the cost of a few seconds of
#   downtime for every site on the server. Pairs with the `lshttpd-pidfile` patch,
#   without which that restart blocks for TimeoutStartSec (90s) and leaves the
#   unit "failed".
#
# Managed file -- edits are lost on reapply, except MODE and DEBOUNCE, which are
# re-read from the installed copy.

MODE=graceful
DEBOUNCE=20

HTTPD_CONF=/etc/apache2/conf/httpd.conf
LSWSCTRL=/usr/local/lsws/bin/lswsctrl
LOCK=/var/run/cpanel_doctor_lsws_ssl_reload.lock
LOG=/var/log/cpanel_doctor_lsws_ssl_reload.log

[ -x "$LSWSCTRL" ] || { echo "1 LiteSpeed not installed, nothing to reload"; exit 0; }

# The worker runs detached (re-exec of this same script) so the hook returns
# immediately and never holds up the AutoSSL run or the WHM API response.
if [ "$1" = "--worker" ]; then
    (
        # Non-blocking: if another invocation holds the lock it is still inside its
        # DEBOUNCE sleep and will reload after this certificate has been written,
        # so there is nothing left for us to do.
        flock -n 9 || exit 0
        sleep "$DEBOUNCE"
        touch "$HTTPD_CONF"
        if [ "$MODE" = "full" ]; then
            systemctl restart lshttpd >/dev/null 2>&1
        else
            "$LSWSCTRL" restart >/dev/null 2>&1
        fi
        echo "$(date '+%F %T') reload (mode=$MODE, debounce=${DEBOUNCE}s)" >>"$LOG"
    ) 9>"$LOCK"
    exit 0
fi

if command -v setsid >/dev/null 2>&1; then
    setsid "$0" --worker >/dev/null 2>&1 &
else
    "$0" --worker >/dev/null 2>&1 &
fi

echo "1 LiteSpeed reload scheduled"
exit 0
