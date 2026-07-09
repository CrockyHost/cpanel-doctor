#!/bin/bash
#
# cPanel Doctor :: post-upcp hook
# ------------------------------------------------------------------------------
# Registered as a cPanel Standardized Hook on System::upcp (post stage). cPanel
# updates overwrite vendor-managed files (and can uninstall packages), silently
# reverting cpanel-doctor patches. After every upcp this restores any enrolled
# patch that was reverted -- surgically, only the missing pieces.
#
# Registered/removed automatically by:  cpanel-doctor hook install | hook remove
#
# cPanel invokes hooks with a minimal PATH; make sure the sbin dirs are present so
# tools like `ip`/`systemctl` resolve (cpanel-doctor also hardens PATH internally).
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/usr/bin:/bin${PATH:+:$PATH}
#
# cPanel passes the hook context as JSON on stdin; we ignore it and just heal.
cat >/dev/null 2>&1   # drain stdin

LOG=/var/log/cpanel-doctor.log
{
    echo "=== $(date '+%F %T') post-upcp: restoring reverted patches ==="
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
