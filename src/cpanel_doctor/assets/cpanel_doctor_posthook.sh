#!/bin/bash
#
# cPanel Doctor :: post-upcp hook
# ------------------------------------------------------------------------------
# Registered as a cPanel Standardized Hook on System::upcp (post stage). cPanel
# updates overwrite vendor-managed files (e.g. phpPgAdmin's config.inc.php), which
# silently un-does parts of some patches. After every upcp this re-applies any
# DRIFTED cpanel-doctor patch -- surgically, only the missing pieces.
#
# Registered/removed automatically by:  cpanel-doctor hook install | hook remove
#
# cPanel passes the hook context as JSON on stdin; we ignore it and just heal.
cat >/dev/null 2>&1   # drain stdin

LOG=/var/log/cpanel-doctor.log
{
    echo "=== $(date '+%F %T') post-upcp: re-applying drifted patches ==="
    # Prefer the installed console script; fall back to module execution.
    if command -v cpanel-doctor >/dev/null 2>&1; then
        cpanel-doctor reapply --yes
    elif command -v cpdoctor >/dev/null 2>&1; then
        cpdoctor reapply --yes
    else
        "${CPANEL_DOCTOR_PYTHON:-python3}" -m cpanel_doctor reapply --yes
    fi
} >>"$LOG" 2>&1

# Standardized hooks must print a JSON result and exit 0.
echo '{"status":1,"message":"cpanel-doctor reapply complete"}'
exit 0
