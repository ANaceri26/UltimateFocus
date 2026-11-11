package cache

import (
	"testing"
	"time"
)

func TestEvictsLeastRecentlyUsed(t *testing.T) {
	c := New[int](2, nil)
	c.Set("a", 1, time.Minute)
	c.Set("b", 2, time.Minute)
	c.Get("a")
	c.Set("c", 3, time.Minute)
	if _, ok := c.Get("b"); ok {
		t.Fatal("b was least recently used and should be gone")
	}
	if v, ok := c.Get("a"); !ok || v != 1 {
		t.Fatal("a should survive")
	}
	if c.Len() != 2 {
		t.Fatalf("len = %d", c.Len())
	}
}

func TestEntriesExpire(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c := New[string](4, func() time.Time { return now })
	c.Set("k", "v", 10*time.Second)
	now = now.Add(9 * time.Second)
	if _, ok := c.Get("k"); !ok {
		t.Fatal("should still be fresh")
	}
	now = now.Add(time.Second)
	if _, ok := c.Get("k"); ok {
		t.Fatal("should have expired")
	}
	if c.Len() != 0 {
		t.Fatal("expired entry should be removed")
	}
}

func TestSetUpdatesExistingKey(t *testing.T) {
	c := New[int](1, nil)
	c.Set("a", 1, time.Minute)
	c.Set("a", 2, time.Minute)
	if v, _ := c.Get("a"); v != 2 || c.Len() != 1 {
		t.Fatal("update failed")
	}
}
