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
	http      *http.Client
	transport *http.Transport
	ua        string
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
	// HTTP2 enables HTTP/2 for upstream calls.
	//
	// The transport below sets a custom DialContext, and the Go standard
	// library conservatively disables HTTP/2 whenever a custom dialer or
	// TLS configuration is present. Left as is, every upstream call over
	// HTTPS pays a separate TCP + TLS handshake. ForceAttemptHTTP2 turns
	// the protocol back on: repeated calls to the same host are multiplexed
	// over one connection.
	//
	// Upstreams that advertise h2 but serve it badly are handled by the
	// standard library (the request fails and the caller sees the error);
	// A_GATEWAY_HTTP2=0 turns the protocol off for those hosts.
	HTTP2 bool
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
		ForceAttemptHTTP2:     opt.HTTP2,
	}
	if !opt.VerifyTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // only with A_GATEWAY_VERIFY=none
	}
	return &Client{
		// No client level timeout: the request deadline is the only budget.
		http: &http.Client{
			Transport:     transport,
			CheckRedirect: sameHostRedirect,
		},
		transport: transport,
		ua:        opt.UserAgent,
	}
}

// sameHostRedirect restricts the redirects to the host of the original
// request.
//
// The Python reference follows the redirects unconditionally (httpx
// follow_redirects=True). This is a deliberate divergence: an official site
// that answers with a redirect to another host (an internal address, for
// example) must not be followed by the gateway, otherwise a caller could
// use a source endpoint to reach an address it may not reach itself.
//
// The host name must stay the same. The port may only change together with
// the scheme, so that an official site which redirects http to https stays
// reachable, while a redirect to another port of the same host is refused
// as well.
//
// 官方源把文件放在自己的 CDN 上时(OFAC 的导出接口用 302 交出带签名的 S3 地址),
// 端点模板要**指向最终地址**; 签名地址一小时就过期, 写不进模板 —— 那种源
// 按"文件型源直连下载"处理(见 ADR-022)。
func sameHostRedirect(req *http.Request, via []*http.Request) error {
	if len(via) == 0 {
		return nil
	}
	if len(via) >= 10 {
		return errors.New("stopped after 10 redirects")
	}
	origin := via[0].URL
	if req.URL.Hostname() != origin.Hostname() {
		return fmt.Errorf("redirect to another host is not followed: %s (the endpoint template must point at the final URL)",
			req.URL.Host)
	}
	if req.URL.Scheme == origin.Scheme && req.URL.Port() != origin.Port() {
		return fmt.Errorf("redirect to another port is not followed: %s", req.URL.Host)
	}
	return nil
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
	return fmt.Sprintf("pool=%d timeout=none http2=%v", c.transport.MaxIdleConnsPerHost, c.transport.ForceAttemptHTTP2)
}
