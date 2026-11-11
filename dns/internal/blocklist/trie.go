// Package blocklist stores domains in a trie keyed by reversed labels.
//
// "www.youtube.com" is stored as com -> youtube -> www. Blocking "youtube.com" marks the
// youtube node as terminal, so every name under it (m.youtube.com, a.b.youtube.com) is
// blocked by one entry. Lookup cost is O(number of labels), independent of list size.
package blocklist

import (
	"strings"
	"sync/atomic"
)

type node struct {
	children map[string]*node
	terminal bool
}

type Trie struct {
	root *node
	size int
}

func New() *Trie { return &Trie{root: &node{}} }

func Build(domains []string) *Trie {
	t := New()
	for _, d := range domains {
		t.Insert(d)
	}
	return t
}

func Normalize(name string) string {
	return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(name), "."))
}

func labels(name string) []string {
	name = Normalize(name)
	if name == "" {
		return nil
	}
	return strings.Split(name, ".")
}

// Insert adds a domain. It returns false for empty or malformed names and duplicates.
func (t *Trie) Insert(domain string) bool {
	parts := labels(domain)
	if len(parts) == 0 {
		return false
	}
	for _, p := range parts {
		if p == "" {
			return false
		}
	}
	n := t.root
	for i := len(parts) - 1; i >= 0; i-- {
		child, ok := n.children[parts[i]]
		if !ok {
			if n.children == nil {
				n.children = make(map[string]*node)
			}
			child = &node{}
			n.children[parts[i]] = child
		}
		n = child
	}
	if n.terminal {
		return false
	}
	n.terminal = true
	t.size++
	return true
}

// Match reports whether name is an inserted domain or a subdomain of one.
func (t *Trie) Match(name string) bool {
	parts := labels(name)
	n := t.root
	for i := len(parts) - 1; i >= 0; i-- {
		child, ok := n.children[parts[i]]
		if !ok {
			return false
		}
		if child.terminal {
			return true
		}
		n = child
	}
	return false
}

func (t *Trie) Len() int { return t.size }

// Store holds the current trie. Readers call Load with no lock; a reload builds a whole
// new trie and swaps the pointer atomically, so a query sees the old list or the new
// one, never a half-built one ("copy-on-write").
type Store struct{ current atomic.Pointer[Trie] }

func NewStore(t *Trie) *Store {
	if t == nil {
		t = New()
	}
	s := &Store{}
	s.current.Store(t)
	return s
}

func (s *Store) Load() *Trie     { return s.current.Load() }
func (s *Store) Replace(t *Trie) { s.current.Store(t) }
