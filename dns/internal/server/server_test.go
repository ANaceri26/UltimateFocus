package server

import (
	"bytes"
	"context"
	"encoding/binary"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"focusdns/internal/blocklist"
	"focusdns/internal/dnsmsg"
)

// fakeUpstream answers every A query with 93.184.216.34, TTL 300.
func fakeUpstream(t *testing.T) (string, *atomic.Int32) {
	t.Helper()
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pc.Close() })
	count := &atomic.Int32{}
	go func() {
		buf := make([]byte, 1500)
		for {
			n, addr, err := pc.ReadFrom(buf)
			if err != nil {
				return
			}
			count.Add(1)
			q, end, err := dnsmsg.ParseQuestion(buf[:n])
			if err != nil {
				continue
			}
			resp := dnsmsg.BlockedResponse(buf[:n], q, end)
			binary.BigEndian.PutUint32(resp[end+6:], 300)
			copy(resp[len(resp)-4:], []byte{93, 184, 216, 34})
			_, _ = pc.WriteTo(resp, addr)
		}
	}()
	return pc.LocalAddr().String(), count
}

func newServer(t *testing.T, mode Mode, events *bytes.Buffer) (*Server, *atomic.Int32) {
	upstream, count := fakeUpstream(t)
	store := blocklist.NewStore(blocklist.Build([]string{"youtube.com"}))
	cfg := Config{Upstream: upstream, Mode: mode, Timeout: time.Second}
	if events != nil {
		cfg.Events = events
	}
	return New(cfg, store), count
}

func answerIP(resp []byte) net.IP { return net.IP(resp[len(resp)-4:]) }

func TestBlockedNameGetsSinkAddressAndIsLogged(t *testing.T) {
	var events bytes.Buffer
	srv, upstreamCalls := newServer(t, ModeBlocklist, &events)
	resp := srv.Handle(dnsmsg.NewQuery(1, "m.youtube.com", dnsmsg.TypeA))
	if !answerIP(resp).Equal(net.IPv4zero) {
		t.Fatalf("want 0.0.0.0, got %v", answerIP(resp))
	}
	if upstreamCalls.Load() != 0 {
		t.Fatal("blocked names must never reach upstream")
	}
	if !strings.Contains(events.String(), "event=dns_blocked domain=m.youtube.com qtype=A") {
		t.Fatalf("event log: %q", events.String())
	}
}

func TestAllowedNameIsForwardedThenCached(t *testing.T) {
	srv, upstreamCalls := newServer(t, ModeBlocklist, nil)
	first := srv.Handle(dnsmsg.NewQuery(10, "example.com", dnsmsg.TypeA))
	second := srv.Handle(dnsmsg.NewQuery(11, "example.com", dnsmsg.TypeA))
	if answerIP(first).String() != "93.184.216.34" {
		t.Fatalf("got %v", answerIP(first))
	}
	if dnsmsg.ID(second) != 11 {
		t.Fatal("cached reply must carry the new query's ID")
	}
	if upstreamCalls.Load() != 1 || srv.Stats.CacheHits.Load() != 1 {
		t.Fatalf("upstream=%d hits=%d", upstreamCalls.Load(), srv.Stats.CacheHits.Load())
	}
}

func TestAllowlistModeBlocksEverythingElse(t *testing.T) {
	srv, _ := newServer(t, ModeAllowlist, nil)
	if !answerIP(srv.Handle(dnsmsg.NewQuery(1, "example.com", dnsmsg.TypeA))).Equal(net.IPv4zero) {
		t.Fatal("names outside the allowlist must be blocked")
	}
	ok := srv.Handle(dnsmsg.NewQuery(2, "www.youtube.com", dnsmsg.TypeA))
	if answerIP(ok).String() != "93.184.216.34" {
		t.Fatal("allowlisted names must resolve")
	}
}

func TestUpstreamFailureGivesServFail(t *testing.T) {
	store := blocklist.NewStore(nil)
	srv := New(Config{Upstream: "127.0.0.1:1", Timeout: 200 * time.Millisecond}, store)
	resp := srv.Handle(dnsmsg.NewQuery(3, "example.com", dnsmsg.TypeA))
	if dnsmsg.Rcode(resp) != dnsmsg.RcodeServFail {
		t.Fatalf("rcode = %d", dnsmsg.Rcode(resp))
	}
}

func TestResponsesAreIgnoredAndJunkGetsFormErr(t *testing.T) {
	srv, _ := newServer(t, ModeBlocklist, nil)
	query := dnsmsg.NewQuery(4, "a.com", dnsmsg.TypeA)
	query[2] |= 0x80
	if srv.Handle(query) != nil {
		t.Fatal("must not answer responses (avoids reflection loops)")
	}
	junk := make([]byte, 12)
	if dnsmsg.Rcode(srv.Handle(junk)) != dnsmsg.RcodeFormErr {
		t.Fatal("want FORMERR")
	}
}

func TestServeOverRealUDP(t *testing.T) {
	srv, _ := newServer(t, ModeBlocklist, nil)
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx, conn) }()

	client, err := net.Dial("udp", conn.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := client.Write(dnsmsg.NewQuery(42, "www.youtube.com", dnsmsg.TypeA)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 512)
	n, err := client.Read(buf)
	if err != nil {
		t.Fatal(err)
	}
	if dnsmsg.ID(buf[:n]) != 42 || !answerIP(buf[:n]).Equal(net.IPv4zero) {
		t.Fatal("bad reply over UDP")
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Serve returned %v", err)
	}
}

func TestLogfmtValueQuotesUnsafeNames(t *testing.T) {
	if logfmtValue("a.com") != "a.com" {
		t.Fatal("safe value changed")
	}
	if got := logfmtValue(`a b"c`); got != `"a b\"c"` {
		t.Fatalf("got %s", got)
	}
}
