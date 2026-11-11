package blocklist

import (
	"strings"
	"sync"
	"testing"
)

func TestMatchCoversSubdomainsButNotLookalikes(t *testing.T) {
	trie := Build([]string{"youtube.com", "Netflix.COM."})
	cases := map[string]bool{
		"youtube.com":      true,
		"www.youtube.com":  true,
		"a.b.youtube.com.": true,
		"YOUTUBE.com":      true,
		"netflix.com":      true,
		"notyoutube.com":   false,
		"youtube.com.evil": false,
		"com":              false,
		"":                 false,
	}
	for name, want := range cases {
		if got := trie.Match(name); got != want {
			t.Errorf("Match(%q) = %v, want %v", name, got, want)
		}
	}
	if trie.Len() != 2 {
		t.Fatalf("Len = %d, want 2", trie.Len())
	}
}

func TestInsertRejectsDuplicatesAndEmptyLabels(t *testing.T) {
	trie := New()
	if !trie.Insert("a.com") || trie.Insert("A.com") {
		t.Fatal("duplicate handling is wrong")
	}
	if trie.Insert("a..com") || trie.Insert("  ") {
		t.Fatal("malformed names must be rejected")
	}
	if trie.Match("x.com") {
		t.Fatal("rejected insert must not leave a terminal node")
	}
}

func TestParseDomainsAcceptsListsAndHostsLines(t *testing.T) {
	input := `# FocusLock list
youtube.com
0.0.0.0 twitch.tv www.twitch.tv   # streaming
142.250.1.1 google.com
::1 localhost
reddit.com news.ycombinator.com
`
	got, err := ParseDomains(strings.NewReader(input))
	if err != nil {
		t.Fatal(err)
	}
	want := "youtube.com twitch.tv www.twitch.tv localhost reddit.com news.ycombinator.com"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q", got)
	}
}

func TestStoreSwapIsSafeUnderConcurrentReads(t *testing.T) {
	store := NewStore(nil)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 1000; j++ {
				store.Load().Match("www.youtube.com")
			}
		}()
	}
	for i := 0; i < 100; i++ {
		store.Replace(Build([]string{"youtube.com"}))
	}
	wg.Wait()
	if !store.Load().Match("m.youtube.com") {
		t.Fatal("expected the latest list to be active")
	}
}
