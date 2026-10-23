// Package upstream performs the outbound HTTP calls of the gateway.
//
// The client has no client level timeout: the per request deadline is the
// only timeout, which is the documented lesson of the Python implementation
// (ADR-007). A client side read timeout would cut off slow but valid
// responses independently of the request budget.
package upstream

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

// ErrTimeout marks an upstream call that exceeded its deadline.
var ErrTimeout = errors.New("upstream timeout")

// Result is the outcome of an upstream call.
type Result struct {
	Status int
	Body   []byte
	// Truncated reports that the body exceeded the byte cap.
	Truncated bool
	// Latency is the wall clock duration of the call.
	Latency time.Duration
}

// Client is the shared upstream HTTP client.
type Client struct {
	http     *http.Client
	transport *http.Transport
	ua       string
}

// Options configures the upstream client.
type Options struct {
	// PoolSize is the number of idle connections kept per host.
	PoolSize int
	// VerifyTLS enables certificate verification. It must only be disabled
	// in a trusted network; A_GATEWAY_VERIFY=none does that.
	VerifyTLS bool
	// UserAgent is the default User-Agent header.
	UserAgent string
	// DefaultHeaders are sent with every request.
	DefaultHeaders map[string]string
}

// New builds the shared upstream client.
func New(opt Options) *Client {
	pool := opt.PoolSize
	if pool <= 0 {
		pool = 32
	}
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:          pool * 2,
		MaxIdleConnsPerHost:   pool,
		MaxConnsPerHost:       pool,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
	if !opt.VerifyTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // only with A_GATEWAY_VERIFY=none
	}
	return &Client{
		// No client level timeout: the request deadline is the only budget.
		http:      &http.Client{Transport: transport},
		transport: transport,
		ua:        opt.UserAgent,
	}
}

// Close releases the idle connections.
func (c *Client) Close() {
	if c.transport != nil {
		c.transport.CloseIdleConnections()
	}
}

// Fetch performs one upstream call.
//
// The body is streamed and cut off at maxBody bytes; the returned Result
// then reports Truncated so that the caller can degrade instead of caching
// a fragment.
func (c *Client) Fetch(ctx context.Context, method, url string, headers map[string]string, maxBody int) (*Result, error) {
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, err
	}
	if c.ua != "" {
		req.Header.Set("User-Agent", c.ua)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	start := time.Now()
	resp, err := c.http.Do(req)
	if err != nil {
		if isTimeout(ctx, err) {
			return nil, ErrTimeout
		}
		return nil, err
	}
	defer resp.Body.Close()

	var body []byte
	truncated := false
	if maxBody > 0 {
		buf, err := io.ReadAll(io.LimitReader(resp.Body, int64(maxBody)+1))
		if err != nil {
			if isTimeout(ctx, err) {
				return nil, ErrTimeout
			}
			return nil, err
		}
		if len(buf) > maxBody {
			truncated = true
			buf = buf[:maxBody]
		}
		body = buf
	} else {
		buf, err := io.ReadAll(resp.Body)
		if err != nil {
			if isTimeout(ctx, err) {
				return nil, ErrTimeout
			}
			return nil, err
		}
		body = buf
	}
	return &Result{
		Status:    resp.StatusCode,
		Body:      body,
		Truncated: truncated,
		Latency:   time.Since(start),
	}, nil
}

func isTimeout(ctx context.Context, err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	if ctx != nil && ctx.Err() == context.DeadlineExceeded {
		return true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	// The Go http client reports a cancelled context as an url.Error.
	msg := err.Error()
	return strings.Contains(msg, "context deadline exceeded") ||
		strings.Contains(msg, "Client.Timeout")
}

// Describe returns a short description of the client configuration, used by
// the startup log.
func (c *Client) Describe() string {
	return fmt.Sprintf("pool=%d timeout=none", c.transport.MaxIdleConnsPerHost)
}
