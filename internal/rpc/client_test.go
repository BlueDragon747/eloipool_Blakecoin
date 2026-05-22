package rpc

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestCallPreservesJSONRPCErrorOnHTTP500(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"result":null,"error":{"code":-8,"message":"block hash unknown"},"id":0}`))
	}))
	defer srv.Close()

	c, err := NewClient(srv.URL, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.Call(context.Background(), "submitauxblock", "hash", "auxpow")
	var rpcErr *RPCError
	if !errors.As(err, &rpcErr) {
		t.Fatalf("expected RPCError, got %T %v", err, err)
	}
	if rpcErr.Code != -8 || rpcErr.Message != "block hash unknown" {
		t.Fatalf("unexpected rpc error: %#v", rpcErr)
	}
}

func TestStatsDoesNotBlockDuringSlowCall(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-release
		_, _ = w.Write([]byte(`{"result":true,"error":null,"id":0}`))
	}))
	defer srv.Close()

	c, err := NewClient(srv.URL, slog.Default())
	if err != nil {
		t.Fatal(err)
	}

	done := make(chan error, 1)
	go func() {
		_, err := c.Call(context.Background(), "slow")
		done <- err
	}()

	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("test server was not called")
	}

	statsDone := make(chan struct{})
	go func() {
		_, _, _ = c.Stats()
		close(statsDone)
	}()

	select {
	case <-statsDone:
	case <-time.After(200 * time.Millisecond):
		t.Fatal("Stats blocked behind in-flight network I/O")
	}

	close(release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("slow call did not finish")
	}
}

func TestCircuitBreakerAccumulatesAcrossReconnectRetries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"result":true,"error":null,"id":0}`))
	}))
	url := srv.URL
	srv.Close()

	c, err := NewClient(url, slog.Default())
	if err != nil {
		t.Fatal(err)
	}

	for i := 0; i < maxConsecutiveFailures; i++ {
		_, _ = c.Call(context.Background(), "fails")
	}

	_, err = c.Call(context.Background(), "breaker")
	var rpcErr *RPCError
	if !errors.As(err, &rpcErr) {
		t.Fatalf("expected RPCError, got %T %v", err, err)
	}
	if rpcErr.Code != -32099 || !strings.Contains(rpcErr.Message, "Circuit breaker open") {
		t.Fatalf("expected open circuit breaker, got %#v", rpcErr)
	}
}
