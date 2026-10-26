// Package cache implements the bounded response cache of the gateway.
//
// The cache is bounded by both the number of entries and the number of
// bytes, expires entries per endpoint TTL and keeps expired entries
// readable so that they can be served as stale-while-revalidate.
package cache

import (
	"container/list"
	"fmt"
	"sync"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// Entry is one cached response.
type Entry struct {
	Value     any
	StoredAt  time.Time
	ExpiresAt time.Time
	Size      int
	Hits      int
	LastHit   time.Time
}

// Stats is the cache statistics block reported by GET /v1/stats.
type Stats struct {
	HotEntries int     `json:"hot_entries"`
	Entries    int     `json:"entries"`
	Bytes      int     `json:"bytes"`
	MaxEntries int     `json:"max_entries"`
	MaxBytes   int     `json:"max_bytes"`
	Hits       int     `json:"hits"`
	Misses     int     `json:"misses"`
	StaleHits  int     `json:"stale_hits"`
	HitRate    float64 `json:"hit_rate"`
}

type node struct {
	key   string
	entry *Entry
}

// Cache is an LRU cache with entry and byte limits.
type Cache struct {
	mu         sync.Mutex
	maxEntries int
	maxBytes   int
	items      map[string]*list.Element
	order      *list.List
	bytes      int
	hits       int
	misses     int
	staleHits  int
}

// New returns a cache bounded by maxEntries and maxBytes.
func New(maxEntries, maxBytes int) *Cache {
	if maxEntries <= 0 {
		maxEntries = 512
	}
	if maxBytes <= 0 {
		maxBytes = 32 * 1024 * 1024
	}
	return &Cache{
		maxEntries: maxEntries,
		maxBytes:   maxBytes,
		items:      map[string]*list.Element{},
		order:      list.New(),
	}
}

// Get returns the cached value, whether it is still fresh and its age in
// seconds. Expired entries are returned as stale values.
func (c *Cache) Get(key string) (any, bool, int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	el, ok := c.items[key]
	if !ok {
		c.misses++
		return nil, false, 0
	}
	n := el.Value.(*node)
	c.order.MoveToBack(el)
	n.entry.Hits++
	n.entry.LastHit = time.Now()
	age := int(time.Since(n.entry.StoredAt).Seconds())
	if time.Now().Before(n.entry.ExpiresAt) {
		c.hits++
		return n.entry.Value, true, age
	}
	c.staleHits++
	return n.entry.Value, false, age
}

// Put stores a value with the given TTL in seconds.
func (c *Cache) Put(key string, value any, ttlSeconds int) {
	size := Size(value)
	if size > c.maxBytes {
		return // a single entry that does not fit is not cached at all
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.bytes -= el.Value.(*node).entry.Size
		c.order.Remove(el)
		delete(c.items, key)
	}
	now := time.Now()
	entry := &Entry{
		Value:     value,
		StoredAt:  now,
		ExpiresAt: now.Add(time.Duration(ttlSeconds) * time.Second),
		Size:      size,
	}
	el := c.order.PushBack(&node{key: key, entry: entry})
	c.items[key] = el
	c.bytes += size
	for c.order.Len() > c.maxEntries || c.bytes > c.maxBytes {
		front := c.order.Front()
		if front == nil {
			break
		}
		fn := front.Value.(*node)
		c.order.Remove(front)
		delete(c.items, fn.key)
		c.bytes -= fn.entry.Size
	}
}

// Entries returns a snapshot of the cached entries.
//
// The map and the Entry structs are copies, but Entry.Value still points at
// the cached object: a caller must treat the value as read only.
func (c *Cache) Entries() map[string]Entry {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]Entry, len(c.items))
	for k, el := range c.items {
		out[k] = *el.Value.(*node).entry
	}
	return out
}

// Hits returns the number of accesses of an entry (used for the adaptive
// TTL: an entry with three or more hits is refreshed with a shorter TTL).
func (c *Cache) Hits(key string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		return el.Value.(*node).entry.Hits
	}
	return 0
}

// Stats returns the cache statistics.
func (c *Cache) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	hot := 0
	for _, el := range c.items {
		if el.Value.(*node).entry.Hits >= 3 {
			hot++
		}
	}
	total := c.hits + c.misses
	rate := 0.0
	if total > 0 {
		rate = float64(c.hits) / float64(total)
	}
	return Stats{
		HotEntries: hot,
		Entries:    c.order.Len(),
		Bytes:      c.bytes,
		MaxEntries: c.maxEntries,
		MaxBytes:   c.maxBytes,
		Hits:       c.hits,
		Misses:     c.misses,
		StaleHits:  c.staleHits,
		HitRate:    round4(rate),
	}
}

// Clear removes every entry.
func (c *Cache) Clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.items = map[string]*list.Element{}
	c.order.Init()
	c.bytes = 0
}

func round4(f float64) float64 {
	return float64(int64(f*10000+0.5)) / 10000
}

// Size measures the serialized size of a value in bytes.
//
// The reference implementation measures the length of the serialized JSON
// document; the Go implementation measures the number of bytes of the same
// document (the reference counts code points, so CJK payloads are counted
// about three times larger there).
func Size(v any) int {
	b, err := parser.Marshal(v)
	if err != nil {
		return len(fmt.Sprintf("%v", v))
	}
	return len(b)
}
