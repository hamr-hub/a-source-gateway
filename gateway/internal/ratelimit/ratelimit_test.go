package ratelimit

import (
	"context"
	"testing"
	"time"
)

func TestGateBoundsConcurrency(t *testing.T) {
	l := New(2)
	if l.Capacity() != 2 {
		t.Errorf("capacity = %d, want 2", l.Capacity())
	}
	release1, err := l.Acquire(context.Background(), "k")
	if err != nil {
		t.Fatal(err)
	}
	release2, err := l.Acquire(context.Background(), "k")
	if err != nil {
		t.Fatal(err)
	}
	if l.InFlight() != 2 {
		t.Errorf("in flight = %d, want 2", l.InFlight())
	}
	// The third call has to wait.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, err := l.Acquire(ctx, "k"); err == nil {
		t.Error("expected the third call to time out")
	}
	release1()
	release2()
	if l.InFlight() != 0 {
		t.Errorf("in flight = %d, want 0", l.InFlight())
	}
}

func TestNoRateLimitByDefault(t *testing.T) {
	l := New(4)
	if l.RateLimited() {
		t.Error("no rate limit must be configured by default")
	}
	release, err := l.Acquire(context.Background(), "crossref")
	if err != nil {
		t.Fatal(err)
	}
	release()
}

func TestTokenBucketThrottles(t *testing.T) {
	l := New(4)
	l.SetRate("slow", 10, 1) // one request per second
	start := time.Now()
	release, err := l.Acquire(context.Background(), "slow")
	if err != nil {
		t.Fatal(err)
	}
	release()
	// The bucket is empty now, the next call has to wait.
	release2, err := l.Acquire(context.Background(), "slow")
	if err != nil {
		t.Fatal(err)
	}
	release2()
	if elapsed := time.Since(start); elapsed < 50*time.Millisecond {
		t.Errorf("elapsed = %v, the second call must wait for a token", elapsed)
	}
}
