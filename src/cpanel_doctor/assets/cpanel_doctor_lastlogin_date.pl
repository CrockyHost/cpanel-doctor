#!/usr/local/cpanel/3rdparty/bin/perl

# cpanel-doctor:lastlogin-date
# ------------------------------------------------------------------------------
# cPanel Doctor :: last-login date fix
#
# cPanel records each login in ~/.lastlogin as "<ip> # <YYYY-MM-DD HH:MM:SS +ZZZZ>"
# (Cpanel::LastLogin::Tiny, compiled into the cpanel and cpsrvd binaries). The
# timestamp comes from the cPanel process clock. On affected hosts that process
# perceives a date in the past (often one frozen instant), so every login is
# recorded with the same wrong date, and Team Manager shows it too.
#
# The file's modification time is set by the kernel from the real clock, so it is
# the true time of the last write, which is the login cPanel just appended as the
# last line. This script copies that mtime into the last line's timestamp when
# the two disagree by more than $TOLERANCE seconds, then puts the mtime back so
# the file reads as untouched on the next pass. Earlier lines are never changed.
# On a host where cPanel's clock is right the two always agree and nothing is
# written.
#
# Safety: home directories belong to customers. Each file is handled with the
# effective uid/gid of the account that owns it, opened with O_NOFOLLOW, and
# skipped unless it is a small regular file owned by that account.
#
# Usage:
#   cpanel_doctor_lastlogin_date.pl            fix every account (run as root)
#   cpanel_doctor_lastlogin_date.pl --check    report only, write nothing
#   cpanel_doctor_lastlogin_date.pl FILE...    fix these files with the current
#                                              privileges (used by the tests)
#
# Installed / removed automatically by:  cpanel-doctor apply|remove lastlogin-date
# Managed by cpanel-doctor -- do not edit by hand.

use strict;
use warnings;

use Fcntl qw(O_RDWR O_RDONLY O_NOFOLLOW O_NONBLOCK S_ISREG SEEK_SET);
use POSIX       ();
use Time::Local ();

my $LOG       = '/var/log/cpanel-doctor.log';
my $USERS_DIR = '/var/cpanel/users';
my $TEAM_DIR  = '/var/cpanel/team';
my $TOLERANCE = 120;          # seconds; a correct clock is always well inside this
my $MAX_SIZE  = 64 * 1024;    # cPanel keeps at most 15 short lines

my $check = 0;
my @files;
for my $arg (@ARGV) {
    if    ( $arg eq '--check' ) { $check = 1 }
    elsif ( $arg =~ /^-/ )      { die "unknown option: $arg\n" }
    else                        { push @files, $arg }
}

# Messages are queued while running as the account and written by _flush_log()
# once root again: the account cannot write the host log.
my @pending_log;

sub _log { push @pending_log, $_[0]; return; }

sub _flush_log {
    my @msgs = splice @pending_log;
    return if $check || @files || !@msgs;    # tests and --check stay out of the host log
    eval {
        open my $fh, '>>', $LOG or return;
        my $ts = localtime();
        print {$fh} "$ts lastlogin-date: $_\n" for @msgs;
        close $fh;
    };
    return;
}

# "2026-10-08 20:06:22 +0300" -> epoch, or undef.
sub _ts_to_epoch {
    my ($ts) = @_;
    my ( $y, $mo, $d, $h, $mi, $s, $sign, $oh, $om ) =
      $ts =~ /\A(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)\z/
      or return undef;
    my $epoch = eval { Time::Local::timegm( $s, $mi, $h, $d, $mo - 1, $y ) };
    return undef if !defined $epoch;
    my $offset = ( $oh * 3600 + $om * 60 ) * ( $sign eq '-' ? -1 : 1 );
    return $epoch - $offset;
}

# Returns 'fixed', 'would-fix', 'ok' or 'skip:<reason>'.
sub fix_file {
    my ( $path, $want_uid ) = @_;

    my $mode = $check ? O_RDONLY : O_RDWR;
    sysopen( my $fh, $path, $mode | O_NOFOLLOW | O_NONBLOCK ) or return 'skip:open';

    my @st = stat($fh);
    return 'skip:stat'      if !@st;
    return 'skip:not-file'  if !S_ISREG( $st[2] );
    return 'skip:not-owner' if defined $want_uid && $st[4] != $want_uid;
    return 'skip:too-big'   if $st[7] > $MAX_SIZE;
    my ( $atime, $mtime ) = @st[ 8, 9 ];

    my $data = '';
    {
        local $/;
        $data = <$fh> // '';
    }
    return 'ok' if $data eq '';

    my @lines = split /\n/, $data, -1;
    pop @lines while @lines && $lines[-1] eq '';    # cPanel writes no trailing newline
    return 'ok' if !@lines;

    my ( $host, $ts ) = $lines[-1] =~ /\A(\S+) # (.+)\z/ or return 'skip:format';
    my $epoch = _ts_to_epoch($ts);
    return 'skip:format' if !defined $epoch;
    return 'ok'          if abs( $mtime - $epoch ) <= $TOLERANCE;
    return 'would-fix'   if $check;

    my $real = POSIX::strftime( '%Y-%m-%d %H:%M:%S %z', localtime $mtime );
    $lines[-1] = "$host # $real";
    my $content = join "\n", @lines;

    seek( $fh, 0, SEEK_SET )             or return 'skip:seek';
    print {$fh} $content                 or return 'skip:write';
    truncate( $fh, length $content )     or return 'skip:truncate';
    utime( $atime, $mtime, $fh );        # put the real login time back (futimens)
    close $fh                            or return 'skip:close';

    _log("$path: last login $ts -> $real");
    return 'fixed';
}

# Run fix_file() with the account's effective uid/gid, then switch back.
sub fix_as {
    my ( $path, $uid, $gid ) = @_;
    return fix_file($path) if $> != 0;    # not root: nothing to drop
    local $) = "$gid $gid";
    local $> = $uid;
    return 'skip:setuid' if $> != $uid;
    return fix_file( $path, $uid );
}

my %count;

if (@files) {
    $count{ fix_file($_) =~ s/:.*//r }++ for @files;
}
else {
    opendir( my $dh, $USERS_DIR ) or exit 0;    # not a cPanel host: nothing to do
    for my $user ( sort readdir $dh ) {
        next if $user =~ /^\./ || $user eq 'system' || $user eq 'root';
        next if !-f "$USERS_DIR/$user";
        my ( $uid, $gid, $home ) = ( getpwnam $user )[ 2, 3, 7 ];
        next if !defined $uid || !$uid || !$home || !-d $home;

        my @paths = ("$home/.lastlogin");
        if ( -e "$TEAM_DIR/$user" ) {    # team users keep $home/<team_user>/.lastlogin
            local $) = "$gid $gid";
            local $> = $uid;
            push @paths, glob("$home/*/.lastlogin") if $> == $uid;
        }
        for my $path (@paths) {
            next if !-e $path && !-l $path;
            my $res = fix_as( $path, $uid, $gid );
            _flush_log();
            $count{ $res =~ s/:.*//r }++;
            print "$res $path\n" if $check && $res ne 'ok';
        }
    }
    closedir $dh;
}

my $summary = join ', ', map { "$_=$count{$_}" } sort keys %count;
print( ( $summary || 'nothing to check' ) . "\n" ) if $check || @files;
exit 0;
