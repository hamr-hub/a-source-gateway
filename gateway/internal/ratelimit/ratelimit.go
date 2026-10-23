// Package ratelimit bounds the outbound traffic of the gateway.
//
// The default behaviour matches the Python reference: only the number of
// concurrent upstream connections is bounded (the httpx connection pool
// limit). Per source request rates are opt-in and disabled by default, so
// the gateway never throttles a request that the Python implementation would
// have served.
package ratelimit

import (
	"context"
	"sync"
	"time"
)

type bucket struct {
	rate   float64 // tokens per second, 0 means unlimited
	burst  float64
	tokens float64
	last   time.Time
}

// Limiter gates concurrent upstream calls and optionally throttles per key
// request rates.
type Limiter struct {
	mu      sync.Mutex
	buckets map[string]*bucket
	sem     chan struct{}
	enabled bool
}

// New returns a limiter that allows poolSize concurrent upstream calls.
func New(poolSize int) *Limiter {
	if poolSize <= 0 {
		poolSize = 32
	}
	return &Limiter{
		buckets: map[string]*bucket{},
		sem:     make(chan struct{}, poolSize),
	}
}

// SetRate configures a request rate for a key (for example a source id).
// A rate of zero disables the limit for that key.
func (l *Limiter) SetRate(key string, ratePerSec float64, burst int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if ratePerSec <= 0 {
		delete(l.buckets, key)
		return
	}
	if burst <= 0 {
		burst = 1
	}
	l.buckets[key] = &bucket{rate: ratePerSec, burst: float64(burst), tokens: float64(burst), last: time.Now()}
	l.enabled = true
}

// Acquire waits for the permission to call the upstream for a key.
//
// It returns a release function that has to be called when the upstream call
// is done. The context is honoured while waiting, so a request that cannot
// be served inside its budget fails with a timeout instead of blocking.
func (l *Limiter) Acquire(ctx context.Context, key string) (func(), error) {
	select {
	case l.sem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	release := func() { <-l.sem }

	wait := l.reserve(key)
	if wait <= 0 {
		return release, nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
		return release, nil
	case <-ctx.Done():
		release()
		return nil, ctx.Err()
	}
}

// reserve consumes one token of the key bucket and returns how long the
// caller has to wait for it.
func (l *Limiter) reserve(key string) time.Duration {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[key]
	if !ok || b.rate <= 0 {
		return 0
	}
	now := time.Now()
	elapsed := now.Sub(b.last).Seconds()
	b.last = now
	b.tokens += elapsed * b.rate
	if b.tokens > b.burst {
		b.tokens = b.burst
	}
	if b.tokens >= 1 {
		b.tokens--
		return 0
	}
	missing := 1 - b.tokens
	return time.Duration(missing / b.rate * float64(time.Second))
}

// InFlight returns the number of running upstream calls.
func (l *Limiter) InFlight() int { return len(l.sem) }

// Capacity returns the number of concurrent upstream calls that are allowed.
func (l *Limiter) Capacity() int { return cap(l.sem) }

// RateLimited reports whether any per key rate limit is configured.
func (l *Limiter) RateLimited() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.enabled
}
