// Package server answers DNS queries over UDP: blocked names get 0.0.0.0, everything
// else is forwarded to an upstream resolver and cached.
package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"focusdns/internal/blocklist"
	"focusdns/internal/cache"
	"focusdns/internal/dnsmsg"
)

type Mode int

const (
	ModeBlocklist Mode = iota
	ModeAllowlist
)

func (m Mode) String() string {
	if m == ModeAllowlist {
		return "allowlist"
	}
	return "blocklist"
}

func ParseMode(s string) (Mode, error) {
	switch strings.ToLower(s) {
	case "blocklist", "":
		return ModeBlocklist, nil
	case "allowlist":
		return ModeAllowlist, nil
	}
	return 0, fmt.Errorf("unknown mode %q (use blocklist or allowlist)", s)
}

type Config struct {
	Upstream    string
	Timeout     time.Duration
	Mode        Mode
	CacheSize   int
	MaxInflight int
	MaxCacheTTL time.Duration
	Events      io.Writer
	Now         func() time.Time
}

type Stats struct {
	Queries        atomic.Uint64
	Blocked        atomic.Uint64
	CacheHits      atomic.Uint64
	UpstreamErrors atomic.Uint64
	Dropped        atomic.Uint64
	ReadErrors     atomic.Uint64
}

type Server struct {
	cfg      Config
	store    *blocklist.Store
	cache    *cache.LRU[[]byte]
	Stats    Stats
	eventsMu sync.Mutex
}

func New(cfg Config, store *blocklist.Store) *Server {
	if cfg.Timeout <= 0 {
		cfg.Timeout = 2 * time.Second
	}
	if cfg.CacheSize <= 0 {
		cfg.CacheSize = 2048
	}
	if cfg.MaxInflight <= 0 {
		cfg.MaxInflight = 256
	}
	if cfg.MaxCacheTTL <= 0 {
		cfg.MaxCacheTTL = time.Hour
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Server{cfg: cfg, store: store, cache: cache.New[[]byte](cfg.CacheSize, cfg.Now)}
}

func (s *Server) blocked(name string) bool {
	if name == "" {
		return false
	}
	match := s.store.Load().Match(name)
	if s.cfg.Mode == ModeAllowlist {
		return !match
	}
	return match
}

func cacheKey(q dnsmsg.Question) string {
	return fmt.Sprintf("%s/%d/%d", q.Name, q.Type, q.Class)
}

// Handle turns one query packet into one response packet (nil means "don't reply").
func (s *Server) Handle(query []byte) []byte {
	s.Stats.Queries.Add(1)
	q, qEnd, err := dnsmsg.ParseQuestion(query)
	if errors.Is(err, dnsmsg.ErrNotQuery) {
		return nil
	}
	if err != nil {
		return dnsmsg.ErrorResponse(query, 0, dnsmsg.RcodeFormErr)
	}
	if s.blocked(q.Name) {
		s.Stats.Blocked.Add(1)
		s.logEvent("dns_blocked", q)
		return dnsmsg.BlockedResponse(query, q, qEnd)
	}
	key := cacheKey(q)
	if cached, ok := s.cache.Get(key); ok {
		s.Stats.CacheHits.Add(1)
		resp := append([]byte(nil), cached...)
		dnsmsg.SetID(resp, dnsmsg.ID(query))
		return resp
	}
	resp, err := s.forward(query)
	if err != nil {
		s.Stats.UpstreamErrors.Add(1)
		return dnsmsg.ErrorResponse(query, qEnd, dnsmsg.RcodeServFail)
	}
	if rc := dnsmsg.Rcode(resp); rc == dnsmsg.RcodeSuccess || rc == dnsmsg.RcodeNXDomain {
		if ttl, ok := dnsmsg.MinTTL(resp); ok && ttl > 0 {
			life := min(time.Duration(ttl)*time.Second, s.cfg.MaxCacheTTL)
			s.cache.Set(key, append([]byte(nil), resp...), life)
		}
	}
	return resp
}

// forward sends the query upstream and waits for the reply with the same ID; replies
// with another ID are ignored (a basic defence against spoofed packets).
func (s *Server) forward(query []byte) ([]byte, error) {
	conn, err := net.Dial("udp", s.cfg.Upstream)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(s.cfg.Timeout)); err != nil {
		return nil, err
	}
	if _, err := conn.Write(query); err != nil {
		return nil, err
	}
	id := dnsmsg.ID(query)
	buf := make([]byte, 65535)
	for {
		n, err := conn.Read(buf)
		if err != nil {
			return nil, err
		}
		if n >= 12 && dnsmsg.ID(buf[:n]) == id && buf[2]&0x80 != 0 {
			return append([]byte(nil), buf[:n]...), nil
		}
	}
}

// Serve reads packets until ctx is cancelled. Each packet is handled in its own
// goroutine; a buffered channel used as a semaphore caps how many run at once, and
// packets beyond that are dropped instead of exhausting memory (back-pressure).
func (s *Server) Serve(ctx context.Context, conn net.PacketConn) error {
	var wg sync.WaitGroup
	defer wg.Wait()
	sem := make(chan struct{}, s.cfg.MaxInflight)
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	buf := make([]byte, 65535)
	for {
		n, addr, err := conn.ReadFrom(buf)
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			// Windows reports WSAECONNRESET on UDP sockets when an earlier reply hit a
			// closed port; that is harmless, so count it and keep serving.
			s.Stats.ReadErrors.Add(1)
			time.Sleep(10 * time.Millisecond)
			continue
		}
		packet := append([]byte(nil), buf[:n]...)
		select {
		case sem <- struct{}{}:
		default:
			s.Stats.Dropped.Add(1)
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			if resp := s.Handle(packet); resp != nil {
				_, _ = conn.WriteTo(resp, addr)
			}
		}()
	}
}

func (s *Server) logEvent(event string, q dnsmsg.Question) {
	if s.cfg.Events == nil {
		return
	}
	line := fmt.Sprintf("ts=%s src=dns event=%s domain=%s qtype=%s\n",
		s.cfg.Now().UTC().Format("2006-01-02T15:04:05Z"), event, logfmtValue(q.Name), dnsmsg.TypeName(q.Type))
	s.eventsMu.Lock()
	defer s.eventsMu.Unlock()
	_, _ = io.WriteString(s.cfg.Events, line)
}

// logfmtValue quotes values that contain anything other than safe characters. DNS
// labels may hold arbitrary bytes, so this stops a crafted name from forging log fields.
func logfmtValue(v string) string {
	safe := v != ""
	for _, r := range v {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("-._:/@+,", r)) {
			safe = false
			break
		}
	}
	if safe {
		return v
	}
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`)
	return `"` + r.Replace(v) + `"`
}
