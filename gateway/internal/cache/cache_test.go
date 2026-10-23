package cache

import (
	"testing"
	"time"
)

func TestGetMiss(t *testing.T) {
	c := New(10, 1024)
	v, fresh, age := c.Get("nope")
	if v != nil || fresh || age != 0 {
		t.Fatalf("got (%v,%v,%v), want (nil,false,0)", v, fresh, age)
	}
	if st := c.Stats(); st.Misses != 1 || st.Hits != 0 {
		t.Errorf("stats = %+v", st)
	}
}

func TestGetFreshAndStale(t *testing.T) {
	c := New(10, 1024)
	c.Put("k", "v", 900)
	v, fresh, age := c.Get("k")
	if v != "v" || !fresh || age != 0 {
		t.Fatalf("got (%v,%v,%v), want (v,true,0)", v, fresh, age)
	}
	if st := c.Stats(); st.Hits != 1 {
		t.Errorf("hits = %d, want 1", st.Hits)
	}

	// An expired entry stays readable and is reported as stale.
	c2 := New(10, 1024)
	c2.Put("k", "v", 0)
	time.Sleep(1100 * time.Millisecond)
	v, fresh, age = c2.Get("k")
	if v != "v" {
		t.Fatalf("stale value = %v, want v", v)
	}
	if fresh {
		t.Error("an expired entry must not be fresh")
	}
	if age < 1 {
		t.Errorf("age = %d, want >= 1", age)
	}
	if st := c2.Stats(); st.StaleHits != 1 {
		t.Errorf("stale_hits = %d, want 1", st.StaleHits)
	}
}

func TestEvictionByEntryCount(t *testing.T) {
	c := New(2, 1<<20)
	c.Put("a", "1", 900)
	c.Put("b", "2", 900)
	c.Put("c", "3", 900)
	st := c.Stats()
	if st.Entries != 2 {
		t.Errorf("entries = %d, want 2", st.Entries)
	}
	if _, _, _ = c.Get("a"); true {
	}
	if c.Stats().Misses != 1 {
		t.Error("the oldest entry must have been evicted")
	}
}

func TestEvictionByByteCount(t *testing.T) {
	// Every value is about 20 bytes, the budget only holds two of them.
	c := New(100, 60)
	c.Put("a", "aaaaaaaaaaaaaaaaaaaa", 900)
	c.Put("b", "bbbbbbbbbbbbbbbbbbbb", 900)
	c.Put("c", "cccccccccccccccccccc", 900)
	st := c.Stats()
	if st.Entries > 3 {
		t.Errorf("entries = %d", st.Entries)
	}
	if st.Bytes > 60 {
		t.Errorf("bytes = %d, want <= 60", st.Bytes)
	}
	if _, _, _ = c.Get("a"); true {
	}
	if c.Stats().Misses != 1 {
		t.Error("the oldest entry must have been evicted by the byte limit")
	}
}

func TestOversizedEntryIsNotCached(t *testing.T) {
	c := New(10, 32)
	c.Put("big", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 900)
	if st := c.Stats(); st.Entries != 0 {
		t.Errorf("entries = %d, want 0", st.Entries)
	}
}

func TestHitsCounting(t *testing.T) {
	c := New(10, 1024)
	c.Put("k", "v", 900)
	if c.Hits("k") != 0 {
		t.Errorf("hits = %d, want 0", c.Hits("k"))
	}
	for i := 0; i < 3; i++ {
		c.Get("k")
	}
	if c.Hits("k") != 3 {
		t.Errorf("hits = %d, want 3", c.Hits("k"))
	}
	if st := c.Stats(); st.HotEntries != 1 {
		t.Errorf("hot_entries = %d, want 1", st.HotEntries)
	}
}

func TestPutReplacesAndKeepsBytes(t *testing.T) {
	c := New(10, 1024)
	c.Put("k", "aaaa", 900)
	c.Put("k", "bb", 900)
	st := c.Stats()
	if st.Entries != 1 {
		t.Errorf("entries = %d, want 1", st.Entries)
	}
	if st.Bytes != 4 { // the JSON document "bb"
		t.Errorf("bytes = %d, want 4", st.Bytes)
	}
}

func TestSizeMeasuresSerializedJSON(t *testing.T) {
	if got := Size("abcd"); got != 6 { // "abcd" with the quotes
		t.Errorf("size = %d, want 6", got)
	}
	if got := Size(map[string]any{"a": 1}); got != 7 { // {"a":1}
		t.Errorf("size = %d, want 7", got)
	}
}
