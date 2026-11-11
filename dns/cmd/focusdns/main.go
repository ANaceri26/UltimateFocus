// Command focusdns is a small filtering DNS resolver for FocusLock.
//
// Point Windows' DNS at 127.0.0.1 and run:
//
//	focusdns -api http://127.0.0.1:8765          # follow FocusLock's blocklist live
//	focusdns -list blocklist.txt                   # or a plain text list
//	focusdns -mode allowlist -list allowed.txt     # only these domains resolve
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"syscall"
	"time"

	"focusdns/internal/blocklist"
	"focusdns/internal/server"
)

type source interface {
	name() string
	domains(ctx context.Context) ([]string, error)
}

type fileSource struct{ path string }

func (f fileSource) name() string { return f.path }

func (f fileSource) domains(context.Context) ([]string, error) {
	file, err := os.Open(f.path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return blocklist.ParseDomains(file)
}

// apiSource polls FocusLock's REST API. When FocusLock isn't enforcing (no session and
// blocking outside sessions is off, or a reward window is open) the list is empty.
type apiSource struct {
	url    string
	token  string
	client *http.Client
}

func (a apiSource) name() string { return a.url }

func (a apiSource) domains(ctx context.Context) ([]string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.url+"/api/v1/blocklist", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API answered %s", resp.Status)
	}
	var body struct {
		Domains   []string `json:"domains"`
		Enforcing bool     `json:"enforcing"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&body); err != nil {
		return nil, fmt.Errorf("bad API response: %w", err)
	}
	if !body.Enforcing {
		return []string{}, nil
	}
	return body.Domains, nil
}

type loader struct {
	store   *blocklist.Store
	sources []source
	last    [][]string
	loaded  []bool
	logger  *log.Logger
}

func newLoader(store *blocklist.Store, sources []source, logger *log.Logger) *loader {
	return &loader{
		store:   store,
		sources: sources,
		last:    make([][]string, len(sources)),
		loaded:  make([]bool, len(sources)),
		logger:  logger,
	}
}

// apply refreshes every source. A source that fails keeps its previous list, so a
// briefly unreachable API does not silently unblock everything.
func (l *loader) apply(ctx context.Context) {
	changed := false
	for i, src := range l.sources {
		domains, err := src.domains(ctx)
		if err != nil {
			l.logger.Printf("source %s: %v (keeping previous list)", src.name(), err)
			continue
		}
		if !l.loaded[i] || !slices.Equal(l.last[i], domains) {
			changed = true
		}
		l.last[i], l.loaded[i] = domains, true
	}
	if !changed {
		return
	}
	var all []string
	for _, domains := range l.last {
		all = append(all, domains...)
	}
	trie := blocklist.Build(all)
	l.store.Replace(trie)
	l.logger.Printf("list loaded: %d domains", trie.Len())
}

func (l *loader) run(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.apply(ctx)
		}
	}
}

func dataDir() string {
	if v := os.Getenv("FOCUSLOCK_HOME"); v != "" {
		return v
	}
	if runtime.GOOS == "windows" {
		if v := os.Getenv("LOCALAPPDATA"); v != "" {
			return filepath.Join(v, "FocusLock")
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "."
	}
	return filepath.Join(home, ".focuslock")
}

func reportStats(ctx context.Context, srv *server.Server, logger *log.Logger) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s := &srv.Stats
			logger.Printf("queries=%d blocked=%d cache_hits=%d upstream_errors=%d dropped=%d",
				s.Queries.Load(), s.Blocked.Load(), s.CacheHits.Load(), s.UpstreamErrors.Load(), s.Dropped.Load())
		}
	}
}

func main() {
	listen := flag.String("listen", "127.0.0.1:53", "UDP address to answer DNS on")
	upstream := flag.String("upstream", "1.1.1.1:53", "upstream DNS server for allowed names")
	listPath := flag.String("list", "", "text file of domains (or hosts-file lines)")
	apiURL := flag.String("api", "", "FocusLock REST API, e.g. http://127.0.0.1:8765")
	tokenFile := flag.String("token-file", "", "API token file (default: <data folder>/api_token)")
	modeFlag := flag.String("mode", "blocklist", "blocklist: block listed names; allowlist: allow only listed names")
	interval := flag.Duration("interval", 10*time.Second, "how often to reload the list")
	cacheSize := flag.Int("cache", 2048, "number of DNS responses to cache")
	logPath := flag.String("log", "", "event log for blocked queries (default: <data folder>/focusdns.log, '-' for none)")
	flag.Parse()

	logger := log.New(os.Stderr, "focusdns: ", log.LstdFlags)
	mode, err := server.ParseMode(*modeFlag)
	if err != nil {
		logger.Fatal(err)
	}
	if *listPath == "" && *apiURL == "" {
		logger.Fatal("give -list FILE and/or -api URL (see -h)")
	}
	if mode == server.ModeAllowlist && *apiURL != "" {
		logger.Fatal("allowlist mode reads its list from -list only")
	}

	var sources []source
	if *listPath != "" {
		sources = append(sources, fileSource{path: *listPath})
	}
	if *apiURL != "" {
		path := *tokenFile
		if path == "" {
			path = filepath.Join(dataDir(), "api_token")
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			logger.Fatalf("read API token: %v (run `focuslock token` once to create it)", err)
		}
		sources = append(sources, apiSource{
			url:    strings.TrimRight(*apiURL, "/"),
			token:  strings.TrimSpace(string(raw)),
			client: &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: nil}},
		})
	}

	var events io.Writer
	if *logPath != "-" {
		path := *logPath
		if path == "" {
			path = filepath.Join(dataDir(), "focusdns.log")
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			logger.Fatal(err)
		}
		file, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err != nil {
			logger.Fatal(err)
		}
		defer file.Close()
		events = file
	}

	conn, err := net.ListenPacket("udp", *listen)
	if err != nil {
		logger.Fatalf("listen on %s: %v (is another DNS service using port 53?)", *listen, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	store := blocklist.NewStore(nil)
	l := newLoader(store, sources, logger)
	l.apply(ctx)
	go l.run(ctx, *interval)

	srv := server.New(server.Config{
		Upstream:  *upstream,
		Mode:      mode,
		CacheSize: *cacheSize,
		Events:    events,
	}, store)
	go reportStats(ctx, srv, logger)

	logger.Printf("answering DNS on %s (upstream %s, %s mode)", *listen, *upstream, mode)
	if err := srv.Serve(ctx, conn); err != nil && !errors.Is(err, context.Canceled) {
		logger.Fatal(err)
	}
	logger.Print("stopped")
}
