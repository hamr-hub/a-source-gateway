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
