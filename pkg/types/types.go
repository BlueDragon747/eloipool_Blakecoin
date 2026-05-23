package types

import "encoding/json"

// JSONRPCRequest represents a JSON-RPC 2.0 request
type JSONRPCRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
	ID      interface{}     `json:"id"`
}

// JSONRPCResponse represents a JSON-RPC 2.0 response
type JSONRPCResponse struct {
	JSONRPC string        `json:"jsonrpc"`
	Result  interface{}   `json:"result"`
	Error   *JSONRPCError `json:"error"`
	ID      interface{}   `json:"id"`
}

// JSONRPCError represents a JSON-RPC 2.0 error
type JSONRPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    string `json:"data,omitempty"`
}

func (e *JSONRPCError) Error() string {
	return e.Message
}

// AuxBlock represents the response from createauxblock.
type AuxBlock struct {
	Hash       string      `json:"hash"`
	Target     string      `json:"target"`
	ChainID    *int        `json:"chainid,omitempty"`
	Version    interface{} `json:"version,omitempty"`
	VersionHex string      `json:"versionHex,omitempty"`
}

// GetAuxResponse represents the response to getaux RPC call
type GetAuxResponse struct {
	Aux             string                   `json:"aux"`
	CoinbaseAux     string                   `json:"coinbaseaux,omitempty"`
	AuxTarget       string                   `json:"aux_target,omitempty"`
	MerkleRoot      string                   `json:"merkle_root,omitempty"`
	Error           string                   `json:"error,omitempty"`
	Warning         string                   `json:"warning,omitempty"`
	ReadyCount      int                      `json:"ready_count"`
	TotalChains     int                      `json:"total_chains"`
	ReadyChains     []map[string]interface{} `json:"ready_chains,omitempty"`
	WaitingChains   []map[string]interface{} `json:"waiting_chains,omitempty"`
	Readiness       []map[string]interface{} `json:"readiness,omitempty"`
	PayoutAddresses []string                 `json:"payout_addresses,omitempty"`
}

// GotWorkRequest represents the params for gotwork RPC call
type GotWorkRequest struct {
	Hash         string `json:"hash"`
	Header       string `json:"header"`
	CoinbaseMrkl string `json:"coinbaseMrkl"`
	Username     string `json:"username,omitempty"`
	ParentValid  bool   `json:"parent_valid,omitempty"`
	ParentStatus string `json:"parent_status,omitempty"`
}

// ProxyStats tracks connection statistics
type ProxyStats struct {
	TotalRequests       int64
	ConsecutiveFailures int64
	LastUsed            int64
}
