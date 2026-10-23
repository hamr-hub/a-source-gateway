package upstream

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestFetchReadsBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "test-agent" {
			t.Errorf("user agent = %q", r.Header.Get("User-Agent"))
		}
		if r.Header.Get("Accept") != "application/json" {
			t.Errorf("accept = %q", r.Header.Get("Accept"))
		}
		w.WriteHeader(http.StatusTeapot)
		_, _ = w.Write([]byte("hello"))
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 4, VerifyTLS: true, UserAgent: "test-agent"})
	res, err := c.Fetch(context.Background(), "GET", srv.URL, map[string]string{"Accept": "application/json"}, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != http.StatusTeapot {
		t.Errorf("status = %d", res.Status)
	}
	if string(res.Body) != "hello" {
		t.Errorf("body = %q", res.Body)
	}
	if res.Truncated {
		t.Error("the body was not truncated")
	}
}

func TestFetchTruncatesAtTheCap(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(strings.Repeat("x", 5000)))
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 2, VerifyTLS: true})
	res, err := c.Fetch(context.Background(), "GET", srv.URL, nil, 100)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Truncated {
		t.Error("the body must be reported as truncated")
	}
	if len(res.Body) != 100 {
		t.Errorf("body length = %d, want 100", len(res.Body))
	}
}

func TestFetchTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(500 * time.Millisecond)
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 2, VerifyTLS: true})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, err := c.Fetch(ctx, "GET", srv.URL, nil, 1024)
	if err == nil {
		t.Fatal("expected a timeout")
	}
	if !errors.Is(err, ErrTimeout) {
		t.Errorf("error = %v, want ErrTimeout", err)
	}
}

func TestFetchConnectionError(t *testing.T) {
	c := New(Options{PoolSize: 2, VerifyTLS: true})
	_, err := c.Fetch(context.Background(), "GET", "http://127.0.0.1:1/nope", nil, 1024)
	if err == nil {
		t.Fatal("expected a connection error")
	}
	if errors.Is(err, ErrTimeout) {
		t.Errorf("error = %v, want a connection error", err)
	}
}

// TestFetchDoesNotFollowARedirectToAnotherHost checks that a redirect to
// another host is not followed: an official site must not be able to send
// the gateway to an internal address.
//
// The Python reference follows the redirects unconditionally; this is a
// deliberate divergence, see sameHostRedirect.
func TestFetchDoesNotFollowARedirectToAnotherHost(t *testing.T) {
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("the internal answer"))
	}))
	defer other.Close()

	// "localhost" is another host name than "127.0.0.1", even when it
	// points at the same machine.
	target := strings.Replace(other.URL, "127.0.0.1", "localhost", 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target+"/secret", http.StatusFound)
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 2, VerifyTLS: true})
	_, err := c.Fetch(context.Background(), "GET", srv.URL, nil, 1024)
	if err == nil {
		t.Fatal("expected an error: the redirect to another host must not be followed")
	}
	if !strings.Contains(err.Error(), "another host") {
		t.Errorf("error = %v, want a cross host redirect error", err)
	}
}

// TestFetchDoesNotFollowARedirectToAnotherPort checks that a redirect to
// another port of the same host is not followed either.
func TestFetchDoesNotFollowARedirectToAnotherPort(t *testing.T) {
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("the internal answer"))
	}))
	defer other.Close()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+"/secret", http.StatusFound)
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 2, VerifyTLS: true})
	_, err := c.Fetch(context.Background(), "GET", srv.URL, nil, 1024)
	if err == nil {
		t.Fatal("expected an error: the redirect to another port must not be followed")
	}
	if !strings.Contains(err.Error(), "another port") {
		t.Errorf("error = %v, want a cross port redirect error", err)
	}
}

// TestFetchFollowsARedirectInsideTheSameHost checks that the redirects
// inside the same host are still followed: an official site that answers
// http with a redirect to https must stay reachable.
func TestFetchFollowsARedirectInsideTheSameHost(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/from" {
			http.Redirect(w, r, "/to", http.StatusFound)
			return
		}
		_, _ = w.Write([]byte("arrived"))
	}))
	defer srv.Close()

	c := New(Options{PoolSize: 2, VerifyTLS: true})
	res, err := c.Fetch(context.Background(), "GET", srv.URL+"/from", nil, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != http.StatusOK {
		t.Errorf("status = %d, want 200", res.Status)
	}
	if string(res.Body) != "arrived" {
		t.Errorf("body = %q, want the answer of the redirect target", res.Body)
	}
}
