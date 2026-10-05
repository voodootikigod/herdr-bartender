package HbSlowRead;
# Test seam for the guard's perl stdin capture (loaded with PERL5OPT=-MHbSlowRead, PERL5LIB=<this dir>):
# the first sysread that returns data then stalls 1.2s, so the 1s capture deadline expires after bytes were
# taken from the pipe and before they were written out. A capture that drops that chunk corrupts the
# vendor's stdin; a correct one writes it before honouring the deadline.
my $stalled = 0;
BEGIN {
    *CORE::GLOBAL::sysread = sub (*\$$;$) {
        my ($fh, $bufref, $len, $off) = @_;
        my $got = CORE::sysread($fh, $$bufref, $len, $off || 0);
        select(undef, undef, undef, 1.2) if $got && !$stalled++;
        return $got;
    };
}
1;
