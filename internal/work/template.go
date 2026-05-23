package work

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"time"

	"github.com/blakecoin/merged-mine-proxy/internal/block"
	"github.com/blakecoin/merged-mine-proxy/internal/rpc"
)

type Template struct {
	Version          uint32
	PreviousBlockHex string
	PreviousBlockLE  []byte
	BitsHex          string
	BitsLE           []byte
	CurTime          uint32
	Height           int64
	CoinbaseValue    int64
	Transactions     []string
	Rules            []string
	Raw              map[string]interface{}
	Updated          time.Time
}

type Job struct {
	ID        string
	Template  *Template
	Coinbase1 string
	Coinbase2 string
	Branches  []string
	TargetHex string
	Created   time.Time
}

type Manager struct {
	client       *rpc.Client
	target       string
	payoutScript []byte
	coinbaseTag  string
	mu           sync.RWMutex
	current      *Job
	seq          uint64
	template     *Template
}

func NewManager(client *rpc.Client) *Manager {
	return &Manager{
		client:       client,
		target:       block.DefaultShareTargetHex,
		payoutScript: []byte{0x51},
		coinbaseTag:  "/GoEloipool/",
	}
}

func (m *Manager) SetPayoutScript(script []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.payoutScript = append([]byte(nil), script...)
}

func (m *Manager) SetCoinbaseTag(tag string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if tag != "" {
		m.coinbaseTag = tag
	}
}

func (m *Manager) SetShareDifficulty(diff int) error {
	target, err := block.ShareTargetForDifficulty(diff)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.target = target
	return nil
}

func (m *Manager) Current() *Job {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.current == nil {
		return nil
	}
	cp := *m.current
	return &cp
}

func (m *Manager) Update() (*Job, error) {
	// Pull the parent Blakecoin template. The aux commitment is added later per
	// miner because per-solver payout addresses can change the aux merkle root.
	raw, err := m.client.Call(context.Background(), "getblocktemplate", map[string]interface{}{
		"capabilities": []string{"coinbasevalue", "coinbase/append", "coinbase", "generation", "time", "transactions/remove", "prevblock"},
		"rules":        []string{"csv", "segwit"},
	})
	if err != nil {
		return nil, err
	}
	tpl, err := ParseTemplate(raw)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.seq++
	job, err := m.buildJobLocked(tpl, "")
	if err != nil {
		return nil, err
	}
	m.template = tpl
	m.current = job
	return job, nil
}

func (m *Manager) JobWithAux(auxHex string) (*Job, error) {
	m.mu.RLock()
	tpl := m.template
	m.mu.RUnlock()
	if tpl == nil {
		return nil, errors.New("no current template")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.buildJobLocked(tpl, auxHex)
}

func (m *Manager) buildJobLocked(tpl *Template, auxHex string) (*Job, error) {
	branches, err := block.MerkleBranches(tpl.Transactions)
	if err != nil {
		return nil, err
	}
	// Coinbase1/2 are split around extranonce fields because Stratum miners
	// supply extranonce2 while the server owns extranonce1.
	parts, err := block.BuildCoinbaseParts(tpl.Height, tpl.CoinbaseValue, m.payoutScript, m.coinbaseTag, auxHex, 8)
	if err != nil {
		return nil, err
	}
	job := &Job{
		ID:        fmt.Sprintf("%d-%d", time.Now().Unix(), m.seq),
		Template:  tpl,
		Coinbase1: parts.Coinbase1,
		Coinbase2: parts.Coinbase2,
		Branches:  branches,
		TargetHex: m.target,
		Created:   time.Now(),
	}
	return job, nil
}

func ParseTemplate(raw json.RawMessage) (*Template, error) {
	var payload map[string]interface{}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	t := &Template{Raw: payload, Updated: time.Now()}
	version, err := asInt64(payload["version"])
	if err != nil {
		return nil, fmt.Errorf("template version: %w", err)
	}
	t.Version = uint32(version)
	t.PreviousBlockHex, _ = payload["previousblockhash"].(string)
	if t.PreviousBlockHex == "" {
		return nil, errors.New("template missing previousblockhash")
	}
	prev, err := hex.DecodeString(t.PreviousBlockHex)
	if err != nil || len(prev) != 32 {
		return nil, fmt.Errorf("invalid previousblockhash %q", t.PreviousBlockHex)
	}
	t.PreviousBlockLE = reverse(prev)
	t.BitsHex, _ = payload["bits"].(string)
	if t.BitsHex == "" {
		return nil, errors.New("template missing bits")
	}
	bits, err := hex.DecodeString(t.BitsHex)
	if err != nil || len(bits) != 4 {
		return nil, fmt.Errorf("invalid bits %q", t.BitsHex)
	}
	t.BitsLE = reverse(bits)
	if cur, err := asInt64(payload["curtime"]); err == nil {
		t.CurTime = uint32(cur)
	}
	if h, err := asInt64(payload["height"]); err == nil {
		t.Height = h
	}
	if cv, err := asInt64(payload["coinbasevalue"]); err == nil {
		t.CoinbaseValue = cv
	}
	if rules, ok := payload["rules"].([]interface{}); ok {
		for _, rule := range rules {
			if s, ok := rule.(string); ok {
				t.Rules = append(t.Rules, s)
			}
		}
	}
	if txs, ok := payload["transactions"].([]interface{}); ok {
		for _, tx := range txs {
			if m, ok := tx.(map[string]interface{}); ok {
				if data, ok := m["data"].(string); ok && data != "" {
					t.Transactions = append(t.Transactions, data)
				}
			}
		}
	}
	return t, nil
}

func asInt64(v interface{}) (int64, error) {
	switch n := v.(type) {
	case float64:
		return int64(n), nil
	case int64:
		return n, nil
	case int:
		return int64(n), nil
	case json.Number:
		return n.Int64()
	case string:
		return strconv.ParseInt(n, 10, 64)
	default:
		return 0, fmt.Errorf("not a number: %T", v)
	}
}

func reverse(in []byte) []byte {
	out := make([]byte, len(in))
	for i := range in {
		out[i] = in[len(in)-1-i]
	}
	return out
}
