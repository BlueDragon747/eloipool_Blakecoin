package rpc

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"
)

const (
	maxRetries             = 2
	dialTimeout            = 8 * time.Second
	requestTimeout         = 35 * time.Second
	idleTimeout            = 30 * time.Minute
	maxConsecutiveFailures = 5
	breakerCooldown        = 15 * time.Second
)

// RPCError preserves daemon JSON-RPC error codes so AuxPoW stale/orphan
// responses can be distinguished from transport faults.
type RPCError struct {
	Code    int
	Message string
	Data    interface{}
}

func (e *RPCError) Error() string {
	if e == nil {
		return ""
	}
	if e.Data != nil {
		return fmt.Sprintf("RPC error %d: %s %v", e.Code, e.Message, e.Data)
	}
	return fmt.Sprintf("RPC error %d: %s", e.Code, e.Message)
}

// Client wraps an HTTP client for JSON-RPC calls with retry, circuit breaker,
// and connection management.
type Client struct {
	url                 string
	path                string
	auth                string
	host                string
	client              *http.Client
	lastUsed            time.Time
	totalRequests       int64
	consecutiveFailures int64
	breakerOpenedAt     time.Time
	mu                  sync.Mutex
	logger              *slog.Logger
}

// NewClient creates a new JSON-RPC client for the given URL.
func NewClient(rpcURL string, logger *slog.Logger) (*Client, error) {
	u, err := url.Parse(rpcURL)
	if err != nil {
		return nil, fmt.Errorf("invalid URL %q: %w", rpcURL, err)
	}

	path := u.Path
	if path == "" {
		path = "/"
	}

	var auth string
	if u.User != nil {
		pw, _ := u.User.Password()
		auth = u.User.Username() + ":" + pw
	}

	host := u.Host
	if host == "" {
		host = "127.0.0.1:8332"
	}

	c := &Client{
		url:    fmt.Sprintf("%s://%s%s", u.Scheme, host, path),
		path:   path,
		auth:   auth,
		host:   host,
		logger: logger,
	}
	c.connectLocked()
	return c, nil
}

func (c *Client) isConnectionStale() bool {
	if c.totalRequests == 0 || c.lastUsed.IsZero() {
		return false
	}
	return time.Since(c.lastUsed) > idleTimeout
}

func (c *Client) connectLocked() {
	c.client = &http.Client{
		Timeout: requestTimeout,
		Transport: &http.Transport{
			DialContext: (&net.Dialer{
				Timeout:   dialTimeout,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			MaxIdleConns:        1,
			MaxIdleConnsPerHost: 1,
			IdleConnTimeout:     idleTimeout,
		},
	}
}

// Call makes a JSON-RPC call with bounded transport retries and a half-open
// circuit breaker. JSON-RPC daemon errors are returned without retry so stale
// AuxPoW submissions are not multiplied.
func (c *Client) Call(ctx context.Context, method string, params ...interface{}) (json.RawMessage, error) {
	var lastErr error
	for attempt := 0; attempt < maxRetries; attempt++ {
		client, err := c.clientForAttempt()
		if err != nil {
			return nil, err
		}

		result, err := c.callSingle(ctx, client, method, params)
		if err == nil {
			c.recordSuccess()
			return result, nil
		}
		if _, ok := err.(*RPCError); ok {
			return nil, err
		}

		lastErr = err
		failures := c.recordFailure()
		c.logger.Warn("RPC call failed", "url", c.url, "method", method, "attempt", attempt+1, "failures", failures, "error", err)

		if attempt < maxRetries-1 {
			time.Sleep(time.Duration(100*(1<<attempt)) * time.Millisecond)
			c.resetConnection()
		}
	}

	return nil, &RPCError{
		Code:    -32099,
		Message: fmt.Sprintf("Could not connect to backend after %d attempts: %v", maxRetries, lastErr),
		Data:    c.url,
	}
}

func (c *Client) clientForAttempt() (*http.Client, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.consecutiveFailures >= maxConsecutiveFailures {
		elapsed := time.Since(c.breakerOpenedAt)
		if elapsed < breakerCooldown {
			return nil, &RPCError{Code: -32099, Message: "Circuit breaker open - too many connection failures", Data: c.url}
		}
		c.logger.Info("Circuit breaker half-open; probing backend", "url", c.url, "elapsed", int(elapsed.Seconds()))
	}

	if c.isConnectionStale() || c.client == nil {
		c.connectLocked()
	}
	return c.client, nil
}

func (c *Client) resetConnection() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.connectLocked()
}

func (c *Client) recordSuccess() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.consecutiveFailures = 0
	c.lastUsed = time.Now()
	c.totalRequests++
}

func (c *Client) recordFailure() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.consecutiveFailures++
	if c.consecutiveFailures >= maxConsecutiveFailures {
		c.breakerOpenedAt = time.Now()
	}
	return c.consecutiveFailures
}

func (c *Client) callSingle(ctx context.Context, client *http.Client, method string, params []interface{}) (json.RawMessage, error) {
	reqBody, err := json.Marshal(map[string]interface{}{
		"jsonrpc": "2.0",
		"method":  method,
		"params":  params,
		"id":      0,
	})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(reqBody))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.auth != "" {
		req.Header.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(c.auth)))
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var rpcResp struct {
		ID     int             `json:"id"`
		Result json.RawMessage `json:"result"`
		Error  *struct {
			Code    int         `json:"code"`
			Message string      `json:"message"`
			Data    interface{} `json:"data,omitempty"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &rpcResp); err != nil {
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return nil, fmt.Errorf("HTTP %d from %s: %s", resp.StatusCode, c.url, string(body))
		}
		return nil, err
	}

	if rpcResp.ID != 0 {
		return nil, fmt.Errorf("invalid id")
	}
	if rpcResp.Error != nil {
		return nil, &RPCError{Code: rpcResp.Error.Code, Message: rpcResp.Error.Message, Data: rpcResp.Error.Data}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d from %s: %s", resp.StatusCode, c.url, string(body))
	}

	return rpcResp.Result, nil
}

// Stats returns connection statistics for logging.
func (c *Client) Stats() (totalRequests, consecutiveFailures, idleSeconds int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	idle := int64(0)
	if !c.lastUsed.IsZero() {
		idle = int64(time.Since(c.lastUsed).Seconds())
	}
	return c.totalRequests, c.consecutiveFailures, idle
}

// Host returns the host:port for logging.
func (c *Client) Host() string { return c.host }
