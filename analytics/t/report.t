use strict;
use warnings;

BEGIN {
    $ENV{TZ} = 'UTC';
}

use FindBin qw($Bin);
use File::Temp qw(tempfile);
use JSON::PP ();
use POSIX ();
use Test::More;

POSIX::tzset() if POSIX->can('tzset');
require "$Bin/../report.pl";

my $rec = parse_line('ts=2026-11-10T14:00:00Z src=app event=session_ended goal="lab \"v2\" done" status=completed');
is($rec->{goal}, 'lab "v2" done', 'quoted values are unescaped');
is($rec->{event}, 'session_ended', 'bare values parse');
ok(!defined parse_line('no fields here'), 'lines without ts/event are ignored');

is(parse_ts('2026-01-01T00:00:10Z'), 1767225610, 'UTC timestamps become epochs');
ok(!defined parse_ts('yesterday'), 'bad timestamps are rejected');

is(longest_streak(qw(2026-01-01 2026-01-02 2026-01-04 2026-01-05 2026-01-06)), 3, 'longest streak');
is(longest_streak(), 0, 'no days, no streak');

my @lines = (
    'ts=2026-11-08T09:30:00Z src=app event=session_ended mode=timer status=completed focused_seconds=5400 reward_seconds=600',
    'ts=2026-11-09T10:00:00Z src=app event=session_ended mode=flow status=completed focused_seconds=1800 reward_seconds=200',
    'ts=2026-11-10T11:00:00Z src=app event=session_ended mode=timer status=abandoned focused_seconds=600 reward_seconds=0',
    'ts=2026-11-10T11:05:00Z src=app event=break_ended skipped=true seconds=3',
    'ts=2026-11-10T11:10:00Z src=app event=break_ended skipped=false seconds=600',
    'ts=2026-11-10T12:00:00Z src=app event=reward_spent seconds=300 window_seconds=300',
    'ts=2026-11-10T12:01:00Z src=dns event=dns_blocked domain=www.youtube.com qtype=A',
    'ts=2026-11-10T12:02:00Z src=dns event=dns_blocked domain=www.youtube.com qtype=AAAA',
    'ts=2026-11-10T12:03:00Z src=dns event=dns_blocked domain=twitch.tv qtype=A',
    'ts=2026-11-10T12:04:00Z src=app event=cheat_recorded kind=password_failed detail=unblock',
    'ts=2026-11-10T12:05:00Z src=app event=cheat_recorded kind=hosts_tampered detail=youtube.com',
    'ts=2026-11-10T12:06:00Z src=app event=cheat_recorded kind=password_failed detail=unblock',
);
my ($fh, $path) = tempfile(UNLINK => 1);
print {$fh} "$_\n" for @lines;
print {$fh} "garbage line\n";
close $fh;

my $records = read_records($path);
is(scalar @$records, scalar @lines, 'reads every valid record');

my $r = aggregate($records);
is($r->{sessions}, 3, 'session count');
is($r->{focused_seconds}, 7800, 'focused total');
is($r->{days}{'2026-11-08'}, 5400, 'per-day totals');
is($r->{hours}[9], 5400, 'per-hour buckets');
is($r->{modes}{flow}, 1800, 'per-mode totals');
is($r->{reward_earned}, 800, 'reward earned');
is($r->{reward_spent}, 300, 'reward spent');
is_deeply([ @$r{qw(breaks_taken breaks_skipped)} ], [1, 1], 'break counts');
is($r->{blocked}{'www.youtube.com'}, 2, 'blocked domains counted');
is($r->{longest_streak}, 3, 'streak across days');
is($r->{cheats}{password_failed}, 2, 'cheats counted by kind');
cmp_ok(abs($r->{completion_rate} - 2 / 3), '<', 1e-9, 'completion rate');

my $since = aggregate($records, since => parse_ts('2026-11-10T00:00:00Z'));
is($since->{sessions}, 1, 'since filter');

my $text = render_text($r, top => 1);
like($text, qr/Sessions:\s+3 \(67% completed\)/, 'text summary');
like($text, qr/www\.youtube\.com/, 'top blocked domain shown');
unlike($text, qr/twitch\.tv/, '--top limits the list');
like($text, qr/Cheats:\s+3/, 'cheat total in summary');

my $decoded = JSON::PP->new->decode(render_json($r));
is($decoded->{focused_seconds}, 7800, 'JSON round trip');
like(render_csv($r), qr/^2026-11-09,1800,30\.0$/m, 'CSV rows');

is(main('--format', 'xml', $path), 2, 'bad format is an error');

done_testing();
