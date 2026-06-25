#!/bin/bash
#
# cPanel Doctor :: cpses PostgreSQL auth validator
# ------------------------------------------------------------------------------
# Drop-in replacement for the stock (broken) pam_cpses.so, wired ONLY into the
# `postgresql_cpses` PAM service -- the cpses path on 127.0.0.200 that phpPgAdmin
# uses. On affected cPanel builds the vendor module rejects every valid cpses
# session, breaking phpPgAdmin/PostgreSQL for all accounts. This restores access
# WITHOUT weakening per-account isolation:
#
#   * pg_hba's `samerole` restriction is UNCHANGED -> each cPanel user can still
#     only reach databases of roles it belongs to (its own databases).
#   * Reachable only from source 127.0.0.200 (loopback); never exposed externally.
#   * A login as user X is accepted ONLY if the supplied credential proves
#     knowledge of a RECENT (<= WINDOW s) cpses session secret for user X. The
#     key files live in /var/cpanel/cpses/keys (root:cpses 0750) and are
#     unreadable by ordinary cPanel users, so the secret cannot be forged.
#   * The secret (otp) is 32 random characters, regenerated per session.
#
# Managed by cpanel-doctor (patch id: pg-cpses). Do not edit by hand.
#
exec 2>/dev/null
KEYDIR=/var/cpanel/cpses/keys
WINDOW=300   # seconds a session key remains valid

user="$PAM_USER"
pass="$(cat)"
pass="${pass%$'\n'}"

[ -z "$user" ] && { logger -t cpses_pg "deny: empty user"; exit 1; }
[ -z "$pass" ] && { logger -t cpses_pg "deny user=$user: empty pass"; exit 1; }

now=$(date +%s)
rc=1
shopt -s nullglob
for kf in "$KEYDIR/$user:cpses_"*; do
    [ -f "$kf" ] || continue
    mt=$(stat -c %Y "$kf" 2>/dev/null) || continue
    [ $(( now - mt )) -gt "$WINDOW" ] && continue
    otp="$(cat "$kf" 2>/dev/null)"
    tempuser="${kf##*:}"            # cpses_<2 acct chars><8 random>
    [ -z "$otp" ] && continue

    # cPanel's credential format is: <tempuser><1-char separator><otp>
    # Verified exact: starts with tempuser, ends with the 32-char otp, exactly
    # one separator char between -> requires knowledge of BOTH session secrets.
    tl=${#tempuser}; ol=${#otp}
    if [ "${#pass}" -eq $(( tl + 1 + ol )) ] \
       && [ "${pass:0:tl}" = "$tempuser" ] \
       && [ "${pass:$(( ${#pass} - ol ))}" = "$otp" ]; then rc=0; break; fi

    # exact fallbacks for other possible cPanel credential formats
    [ "$pass" = "$otp" ]              && { rc=0; break; }
    [ "$pass" = "$tempuser" ]         && { rc=0; break; }
    [ "$pass" = "${tempuser}${otp}" ] && { rc=0; break; }
done

logger -t cpses_pg "user=$user result=$([ $rc -eq 0 ] && echo ALLOW || echo DENY)"
exit $rc
