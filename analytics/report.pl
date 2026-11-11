#!/usr/bin/env perl
# FocusLock productivity report.
#
# Reads the logfmt event logs written by FocusLock (focuslock.log) and the Go resolver
# (focusdns.log), aggregates them and prints a report as text, JSON or CSV.
#
#   perl analytics/report.pl                   # last 7 days from the default data folder
#   perl analytics/report.pl --days 30 --format json
#   perl analytics/report.pl --all path/to/focuslock.log path/to/focusdns.log
#
# Uses only modules that ship with Perl (Getopt::Long, JSON::PP, POSIX, Time::Local).
use strict;
use warnings;

use File::Spec;
use Getopt::Long qw(GetOptionsFromArray);
use JSON::PP ();
use POSIX qw(strftime);
use Time::Local qw(timegm);

our $VERSION = '0.3.0';
my $BAR_WIDTH = 40;
my $LOG_NAME  = qr/\A(?:focuslock|focusdns)\.log(?:\.\d+)?\z/;

# key=value or key="quoted \"value\"" pairs.
sub parse_line {
    my ($line) = @_;
    my %field;
    while ($line =~ /(\w+)=("(?:[^"\\]|\\.)*"|\S*)/g) {
        my ($key, $value) = ($1, $2);
        if ($value =~ /\A"(.*)"\z/s) {
            $value = $1;
            $value =~ s/\\(.)/$1 eq 'n' ? "\n" : $1/ge;
        }
        $field{$key} = $value;
    }
    return (exists $field{ts} && exists $field{event}) ? \%field : undef;
}

sub parse_ts {
    my ($ts) = @_;
    return undef unless defined $ts && $ts =~ /\A(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z\z/;
    return timegm($6, $5, $4, $3, $2 - 1, $1);
}

sub num {
    my ($value) = @_;
    return (defined $value && $value =~ /\A-?\d+(?:\.\d+)?\z/) ? $value + 0 : 0;
}

sub data_dir {
    return $ENV{FOCUSLOCK_HOME} if $ENV{FOCUSLOCK_HOME};
    if ($^O eq 'MSWin32' && $ENV{LOCALAPPDATA}) {
        return File::Spec->catdir($ENV{LOCALAPPDATA}, 'FocusLock');
    }
    return File::Spec->catdir($ENV{HOME} // $ENV{USERPROFILE} // '.', '.focuslock');
}

sub default_log_files {
    my $dir = data_dir();
    opendir(my $dh, $dir) or return ();
    my @files = map { File::Spec->catfile($dir, $_) } sort grep { $_ =~ $LOG_NAME } readdir $dh;
    closedir $dh;
    return @files;
}

sub read_records {
    my @files = @_;
    my @records;
    for my $file (@files) {
        open(my $fh, '<:encoding(UTF-8)', $file) or do { warn "skipping $file: $!\n"; next };
        while (my $line = <$fh>) {
            chomp $line;
            my $record = parse_line($line);
            push @records, $record if $record;
        }
        close $fh;
    }
    return \@records;
}

sub day_epoch {
    my ($day) = @_;
    my ($y, $m, $d) = split /-/, $day;
    return timegm(0, 0, 0, $d, $m - 1, $y);
}

sub longest_streak {
    my @days = sort @_;
    my ($best, $run, $previous) = (0, 0, undef);
    for my $day (@days) {
        my $epoch = day_epoch($day);
        $run = (defined $previous && $epoch - $previous == 86_400) ? $run + 1 : 1;
        $best = $run if $run > $best;
        $previous = $epoch;
    }
    return $best;
}

# Builds one hash of totals. %$r{days} is a hash keyed by local date; hours is an array
# of 24 buckets; blocked counts DNS-blocked lookups per domain.
sub aggregate {
    my ($records, %opt) = @_;
    my $since = $opt{since} // 0;
    my %r = (
        days            => {},
        hours           => [(0) x 24],
        modes           => {},
        statuses        => {},
        blocked         => {},
        cheats          => {},
        sessions        => 0,
        focused_seconds => 0,
        reward_earned   => 0,
        reward_spent    => 0,
        breaks_taken    => 0,
        breaks_skipped  => 0,
    );
    for my $rec (@$records) {
        my $epoch = parse_ts($rec->{ts});
        next unless defined $epoch && $epoch >= $since;
        my @local = localtime $epoch;
        my $day   = strftime('%Y-%m-%d', @local);
        my $event = $rec->{event};
        if ($event eq 'session_ended') {
            my $seconds = num($rec->{focused_seconds});
            $r{sessions}++;
            $r{focused_seconds} += $seconds;
            $r{days}{$day} += $seconds;
            $r{hours}[ $local[2] ] += $seconds;
            $r{modes}{ $rec->{mode} // 'unknown' } += $seconds;
            $r{statuses}{ $rec->{status} // 'unknown' }++;
            $r{reward_earned} += num($rec->{reward_seconds});
        }
        elsif ($event eq 'break_ended') {
            if (($rec->{skipped} // '') eq 'true') { $r{breaks_skipped}++ }
            else                                   { $r{breaks_taken}++ }
        }
        elsif ($event eq 'reward_spent') {
            $r{reward_spent} += num($rec->{seconds});
        }
        elsif ($event eq 'cheat_recorded') {
            $r{cheats}{ $rec->{kind} // 'unknown' }++;
        }
        elsif ($event eq 'dns_blocked' && defined $rec->{domain}) {
            $r{blocked}{ $rec->{domain} }++;
        }
    }
    my $completed = $r{statuses}{completed} // 0;
    $r{completion_rate} = $r{sessions} ? $completed / $r{sessions} : 0;
    $r{longest_streak} = longest_streak(grep { $r{days}{$_} > 0 } keys %{ $r{days} });
    return \%r;
}

sub hm {
    my $seconds = int(shift // 0);
    return sprintf('%dh %02dm', int($seconds / 3600), int(($seconds % 3600) / 60));
}

sub bar {
    my ($value, $peak) = @_;
    return '' unless $peak;
    return '#' x int($value / $peak * $BAR_WIDTH + 0.5);
}

sub max0 {
    my $max = 0;
    for (@_) { $max = $_ if $_ > $max }
    return $max;
}

sub top_blocked {
    my ($blocked, $limit) = @_;
    my @names = sort { $blocked->{$b} <=> $blocked->{$a} || $a cmp $b } keys %$blocked;
    splice(@names, $limit) if @names > $limit;
    return @names;
}

sub render_text {
    my ($r, %opt) = @_;
    my $top = $opt{top} // 5;
    my @out = ('FocusLock productivity report', '=' x 29, '');
    push @out, sprintf('Sessions:        %d (%.0f%% completed)', $r->{sessions}, 100 * $r->{completion_rate});
    push @out, sprintf('Focused time:    %s', hm($r->{focused_seconds}));
    push @out, sprintf('Longest streak:  %d day(s)', $r->{longest_streak});
    push @out, sprintf('Breaks:          %d taken, %d skipped', $r->{breaks_taken}, $r->{breaks_skipped});
    push @out, sprintf('Reward time:     %s earned, %s spent', hm($r->{reward_earned}), hm($r->{reward_spent}));
    my $cheat_total = 0;
    $cheat_total += $_ for values %{ $r->{cheats} };
    push @out, sprintf('Cheats:          %d', $cheat_total);

    push @out, '', 'Focus per day';
    my @days = sort keys %{ $r->{days} };
    my $peak = max0(map { $r->{days}{$_} } @days);
    push @out, sprintf('  %s  %-*s %s', $_, $BAR_WIDTH, bar($r->{days}{$_}, $peak), hm($r->{days}{$_})) for @days;
    push @out, '  (no sessions logged in this period)' unless @days;

    my @hours = grep { $r->{hours}[$_] > 0 } 0 .. 23;
    if (@hours) {
        push @out, '', 'When you focus (by end hour)';
        my $hour_peak = max0(@{ $r->{hours} });
        push @out, sprintf('  %02d:00  %-*s %s', $_, $BAR_WIDTH, bar($r->{hours}[$_], $hour_peak), hm($r->{hours}[$_])) for @hours;
    }

    if (%{ $r->{modes} }) {
        push @out, '', 'By mode';
        push @out, sprintf('  %-6s %s', $_, hm($r->{modes}{$_})) for sort keys %{ $r->{modes} };
    }

    if ($cheat_total) {
        push @out, '', 'Cheat log';
        push @out, sprintf('  %-24s %d', $_, $r->{cheats}{$_}) for sort keys %{ $r->{cheats} };
    }

    my @blocked = top_blocked($r->{blocked}, $top);
    if (@blocked) {
        push @out, '', "Most blocked domains (DNS resolver, top $top)";
        push @out, sprintf('  %6d  %s', $r->{blocked}{$_}, $_) for @blocked;
    }
    return join("\n", @out) . "\n";
}

sub render_json {
    my ($r) = @_;
    return JSON::PP->new->canonical->pretty->encode($r);
}

sub render_csv {
    my ($r) = @_;
    my @lines = ('day,focused_seconds,focused_minutes');
    for my $day (sort keys %{ $r->{days} }) {
        my $seconds = $r->{days}{$day};
        push @lines, sprintf('%s,%d,%.1f', $day, $seconds, $seconds / 60);
    }
    return join("\n", @lines) . "\n";
}

sub start_of_window {
    my ($days) = @_;
    my @lt = localtime;
    my $midnight = time - ($lt[2] * 3600 + $lt[1] * 60 + $lt[0]);
    return $midnight - ($days - 1) * 86_400;
}

sub usage {
    my ($code) = @_;
    my $fh = $code ? \*STDERR : \*STDOUT;
    print {$fh} <<'USAGE';
Usage: perl report.pl [--days N | --all] [--format text|json|csv] [--top N] [LOG...]

With no LOG files, reads focuslock.log* and focusdns.log* from the FocusLock data
folder (FOCUSLOCK_HOME, else %LOCALAPPDATA%\FocusLock, else ~/.focuslock).
USAGE
    return $code;
}

sub main {
    my @argv = @_;
    my %opt = (days => 7, format => 'text', top => 5);
    GetOptionsFromArray(
        \@argv,
        'days=i'   => \$opt{days},
        'all'      => \$opt{all},
        'format=s' => \$opt{format},
        'top=i'    => \$opt{top},
        'help|h'   => \$opt{help},
    ) or return usage(2);
    return usage(0) if $opt{help};
    if ($opt{format} !~ /\A(?:text|json|csv)\z/) {
        warn "unknown --format '$opt{format}' (use text, json or csv)\n";
        return 2;
    }
    if ($opt{days} < 1) {
        warn "--days must be at least 1\n";
        return 2;
    }
    my @files = @argv ? @argv : default_log_files();
    unless (@files) {
        warn "No FocusLock logs found in " . data_dir() . ". Run FocusLock first, or pass log paths.\n";
        return 1;
    }
    my $report = aggregate(read_records(@files), since => $opt{all} ? 0 : start_of_window($opt{days}));
    binmode STDOUT, ':encoding(UTF-8)';
    print $opt{format} eq 'json' ? render_json($report)
        : $opt{format} eq 'csv'  ? render_csv($report)
        :                          render_text($report, top => $opt{top});
    return 0;
}

# "Modulino": run main() when executed, but only define the subs when loaded by a test.
exit main(@ARGV) unless caller;

1;
