#!/usr/local/cpanel/3rdparty/bin/perl

# cpanel-doctor:account-startdate
# ------------------------------------------------------------------------------
# cPanel Doctor :: account creation-date fix
#
# Registered as a cPanel Standardized Hook on Whostmgr::Accounts::Create (post
# stage). On affected hosts the account creation date (STARTDATE in
# /var/cpanel/users/<user>) is written with a timestamp in the *past* at
# creation time, even though the OS clock is correct -- so freshly created
# accounts show a wrong creation date in WHM. This hook runs right after an
# account is created and rewrites STARTDATE to the real "now".
#
# The true current time is read from a CLEAN child process (env -i + /bin/date),
# bypassing any per-process time override (e.g. an LD_PRELOAD/libfaketime
# inherited from the account-creation process): a plain `date` on the host is
# correct, so dropping the inherited environment is enough to get the real
# epoch. Falls back to the in-process time() if the clock can't be read.
#
# Writing through Cpanel::Config::CpUserGuard updates BOTH the datastore
# (/var/cpanel/users/<user>) and the cache (/var/cpanel/users.cache/<user>).
#
# Installed / removed automatically by:  cpanel-doctor apply|remove account-startdate
# Managed by cpanel-doctor -- do not edit by hand.
#
# Fail-safe: any problem is logged and the hook still reports success (post
# stage; it must never disrupt account creation).

use strict;
use warnings;

use Cpanel::JSON                ();
use Cpanel::Config::CpUserGuard ();

my $LOG = '/var/log/cpanel-doctor.log';

# Best-effort log line; never fatal.
sub _log {
    my ($msg) = @_;
    eval {
        open my $fh, '>>', $LOG or return;
        my $ts = localtime();
        print {$fh} "$ts account-startdate: $msg\n";
        close $fh;
    };
    return;
}

# Always report success to cPanel (first line "1 <message>"), then exit.
my $done = sub {
    my ($msg) = @_;
    _log($msg);
    print "1 account-startdate: $msg\n";
    exit 0;
};

# ---- read the hook payload (JSON on STDIN) ---------------------------------
my $raw = do { local $/; <STDIN> };
my $payload = eval { Cpanel::JSON::Load($raw) } || {};

# Accounts::Create delivers the new account name under data.user.
my $user =
       $payload->{'data'}{'user'}
    // $payload->{'data'}{'username'}
    // $payload->{'user'};

$done->("no user in payload, nothing to do") if !defined $user || $user eq '';

$user =~ s/[^A-Za-z0-9._-]//g;    # sanitize
$done->("invalid user name, skipping")        if $user eq '';
$done->("no datastore for '$user', skipping") if !-e "/var/cpanel/users/$user";

# ---- true current epoch, bypassing any inherited time faking ---------------
my $now = '';
{
    my $out = `/usr/bin/env -i /bin/date +%s 2>/dev/null`;
    ($now) = ( ( $out // '' ) =~ /(\d{9,})/ );
}
$now ||= time();    # last-resort fallback

# ---- correct STARTDATE only if it is clearly wrong (off by > 1 day) --------
my $guard = eval { Cpanel::Config::CpUserGuard->new($user) }
    or $done->("could not open datastore for '$user'");

my $cur = $guard->{'data'}{'STARTDATE'} // 0;

if ( abs( $now - $cur ) <= 86400 ) {
    $done->("STARTDATE for '$user' already current ($cur), leaving as-is");
}

$guard->{'data'}{'STARTDATE'} = $now;

eval { $guard->save(); 1 }
    or $done->("save failed for '$user': $@");

$done->("STARTDATE for '$user' corrected: $cur -> $now");
