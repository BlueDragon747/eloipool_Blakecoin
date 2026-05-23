package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/blakecoin/merged-mine-proxy/internal/config"
	"github.com/blakecoin/merged-mine-proxy/internal/health"
	"github.com/blakecoin/merged-mine-proxy/internal/merkle"
	"github.com/blakecoin/merged-mine-proxy/internal/rpc"
	"github.com/blakecoin/merged-mine-proxy/pkg/types"
)

func TestAuxTemplateVersionAcceptsIntegerOrHex(t *testing.T) {
	version, versionHex, ok := auxTemplateVersion(types.AuxBlock{Version: float64(0x20020104)})
	if !ok || version != 0x20020104 || versionHex != "20020104" {
		t.Fatalf("unexpected version parse: %x %s %v", version, versionHex, ok)
	}

	version, versionHex, ok = auxTemplateVersion(types.AuxBlock{VersionHex: "20020104"})
	if !ok || version != 0x20020104 || versionHex != "20020104" {
		t.Fatalf("unexpected versionHex parse: %x %s %v", version, versionHex, ok)
	}
}

func TestTaprootStartedRequiresTemplateBit(t *testing.T) {
	failures := requiredBIP9SignalFailures(
		types.AuxBlock{Version: float64(0x20020100)},
		map[string]interface{}{"taproot": deployment("defined", "started")},
	)
	if len(failures) != 1 {
		t.Fatalf("expected one failure, got %v", failures)
	}
}

func TestTaprootLockedInAcceptsTemplateBit(t *testing.T) {
	failures := requiredBIP9SignalFailures(
		types.AuxBlock{Version: float64(0x20020104)},
		map[string]interface{}{"taproot": deployment("started", "locked_in")},
	)
	if len(failures) != 0 {
		t.Fatalf("expected no failures, got %v", failures)
	}
}

func TestTaprootActiveDoesNotRequireTemplateBit(t *testing.T) {
	failures := requiredBIP9SignalFailures(
		types.AuxBlock{Version: float64(0x20020100)},
		map[string]interface{}{"taproot": deployment("locked_in", "active")},
	)
	if len(failures) != 0 {
		t.Fatalf("expected no failures, got %v", failures)
	}
}

func TestAuxVersionSummaryNamesSignals(t *testing.T) {
	chainID := 2
	summary := summarizeAuxVersionBits(types.AuxBlock{
		Version:    float64(0x20020104),
		VersionHex: "20020104",
		ChainID:    &chainID,
	}, chainID)
	want := "version=20020104 top=bip9 auxpow=yes chain_id=2 signals=taproot"
	if summary != want {
		t.Fatalf("got %q, want %q", summary, want)
	}
}

func TestStaleAuxSubmissionErrors(t *testing.T) {
	if !isStaleAuxSubmissionError(&rpc.RPCError{Code: -8, Message: "block hash unknown"}) {
		t.Fatal("expected stale block hash unknown")
	}
	if isStaleAuxSubmissionError(&rpc.RPCError{Code: -1, Message: "misc error"}) {
		t.Fatal("unexpected stale classification for misc error")
	}
	if !isStaleAuxSubmissionError(fmt.Errorf("wrapped: %w", &rpc.RPCError{Code: -8, Message: "block hash unknown"})) {
		t.Fatal("expected wrapped stale block hash unknown")
	}
}

func TestChooseMerkleNonceAvoidsCollisions(t *testing.T) {
	l := &Listener{merkleSize: 16}
	a, b, c := 1, 2, 3
	nonce, err := l.chooseMerkleNonce([]*int{&a, &b, &c})
	if err != nil {
		t.Fatal(err)
	}
	seen := map[int]bool{}
	for _, id := range []int{a, b, c} {
		idx := l.calcMerkleIndexFor(id, nonce)
		if seen[idx] {
			t.Fatalf("collision at idx %d for nonce %d", idx, nonce)
		}
		seen[idx] = true
	}
}

func TestPerSolverTemplateDoesNotCommitGlobalStateOrHealth(t *testing.T) {
	aux0 := jsonRPCServer(t, map[string]rpcReply{
		"createauxblock": {
			result: map[string]interface{}{
				"hash":    fmt.Sprintf("%064x", 1),
				"target":  fmt.Sprintf("%064x", 2),
				"chainid": 42,
				"version": 0x20020104,
			},
		},
		"getdeploymentinfo": {
			result: map[string]interface{}{
				"deployments": map[string]interface{}{
					"taproot": deployment("locked_in", "active"),
				},
			},
		},
	})
	defer aux0.Close()
	aux1 := jsonRPCServer(t, map[string]rpcReply{
		"createauxblock": {errCode: -32603, errMessage: "backend down"},
	})
	defer aux1.Close()
	parent := jsonRPCServer(t, map[string]rpcReply{})
	defer parent.Close()

	l, err := NewListener(&config.Config{
		ParentURLs:         []string{parent.URL},
		AuxURLs:            []string{aux0.URL, aux1.URL},
		AuxPayoutAddresses: []string{"blc1qpoolpayout", "pho1qpoolpayout"},
		MerkleSize:         2,
	}, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	l.healthTracker.MarkUnhealthy(0)

	result := l.buildAuxTemplate(context.Background(), []string{"blc1qpoolpayout", "pho1qpoolpayout"}, false)
	if result["merkle_root"] == nil {
		t.Fatalf("expected per-solver merkle root, got %#v", result)
	}
	if l.healthTracker.IsHealthy(0) {
		t.Fatal("per-solver template fetch should not mark global chain health healthy")
	}

	l.mu.RLock()
	defer l.mu.RUnlock()
	if l.chainIDs[0] != nil || l.chainIDs[1] != nil {
		t.Fatalf("per-solver template committed global chain IDs: %#v", l.chainIDs)
	}
	if l.auxTargets[0] != "" || l.auxTargets[1] != "" {
		t.Fatalf("per-solver template committed global targets: %#v", l.auxTargets)
	}
}

func TestNewListenerRequiresAuxPayoutAddressPerAuxURL(t *testing.T) {
	parent := jsonRPCServer(t, map[string]rpcReply{})
	defer parent.Close()
	aux := jsonRPCServer(t, map[string]rpcReply{})
	defer aux.Close()

	_, err := NewListener(&config.Config{
		ParentURLs: []string{parent.URL},
		AuxURLs:    []string{aux.URL},
		MerkleSize: 1,
	}, testLogger())
	if err == nil {
		t.Fatal("expected missing aux payout address to fail listener creation")
	}
}

func TestNewListenerUsesConfiguredAuxChainNames(t *testing.T) {
	parent := jsonRPCServer(t, map[string]rpcReply{})
	defer parent.Close()
	aux0 := jsonRPCServer(t, map[string]rpcReply{})
	defer aux0.Close()
	aux1 := jsonRPCServer(t, map[string]rpcReply{})
	defer aux1.Close()

	l, err := NewListener(&config.Config{
		ParentURLs:         []string{parent.URL},
		AuxURLs:            []string{aux0.URL, aux1.URL},
		AuxPayoutAddresses: []string{"pho1qpoolpayout", "elt1qpoolpayout"},
		AuxChainNames:      []string{"Photon", "Electron"},
		MerkleSize:         2,
	}, testLogger())
	if err != nil {
		t.Fatalf("NewListener failed: %v", err)
	}
	status := l.rpcStatus()
	readiness := status["readiness"].([]map[string]interface{})
	if readiness[0]["chain"] != "Photon" || readiness[1]["chain"] != "Electron" {
		t.Fatalf("unexpected chain names: %#v", readiness)
	}
}

func TestFetchAuxDeploymentsUsesShortCache(t *testing.T) {
	var deploymentCalls atomic.Int64
	parent := jsonRPCServer(t, map[string]rpcReply{})
	defer parent.Close()
	aux := jsonRPCServer(t, map[string]rpcReply{
		"createauxblock": {
			result: map[string]interface{}{
				"hash":    fmt.Sprintf("%064x", 1),
				"target":  fmt.Sprintf("%064x", 2),
				"chainid": 42,
				"version": 0x20020104,
			},
		},
		"getdeploymentinfo": {
			result: map[string]interface{}{
				"deployments": map[string]interface{}{
					"taproot": deployment("locked_in", "active"),
				},
			},
			count: &deploymentCalls,
		},
	})
	defer aux.Close()

	l, err := NewListener(&config.Config{
		ParentURLs:         []string{parent.URL},
		AuxURLs:            []string{aux.URL},
		AuxPayoutAddresses: []string{"bbtc1qpoolpayout"},
		MerkleSize:         1,
	}, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		result := l.fetchAuxTemplate(context.Background(), 0, "bbtc1qpoolpayout", true)
		if !result.ready {
			t.Fatalf("template fetch %d not ready: %#v", i, result)
		}
	}
	if deploymentCalls.Load() != 1 {
		t.Fatalf("deployment calls = %d, want 1", deploymentCalls.Load())
	}
}

func TestGotworkSkipsChainsMissingFromTemplate(t *testing.T) {
	var submitted0 atomic.Int64
	var submitted1 atomic.Int64
	aux0 := jsonRPCServer(t, map[string]rpcReply{
		"submitauxblock": {result: true, count: &submitted0},
	})
	defer aux0.Close()
	aux1 := jsonRPCServer(t, map[string]rpcReply{
		"submitauxblock": {result: true, count: &submitted1},
	})
	defer aux1.Close()
	client0, err := rpc.NewClient(aux0.URL, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	client1, err := rpc.NewClient(aux1.URL, testLogger())
	if err != nil {
		t.Fatal(err)
	}

	leaf0 := fmt.Sprintf("%064x", 1)
	leaf1 := fmt.Sprintf("%064x", 2)
	mt, err := merkle.NewTreeChecked([]string{leaf0, leaf1})
	if err != nil {
		t.Fatal(err)
	}
	root := mt.Root()
	chainID1 := 2
	l := &Listener{
		auxs:          []*rpc.Client{client0, client1},
		chainIDs:      []*int{nil, &chainID1},
		healthTracker: health.NewTracker(),
		merkleSize:    2,
		merkleTrees: map[string]*merkleMeta{
			root: {
				Tree:            mt.Detail(),
				Nonce:           0,
				ChainIndices:    map[int]int{0: 0},
				AuxHashes:       map[int]string{0: leaf0},
				PayoutAddresses: []string{"blc1qpoolpayout", "pho1qpoolpayout"},
			},
		},
		logger: testLogger(),
	}

	coinbase := "00fabe6d6d" + root + encodeLE32Hex(uint32(l.merkleSize)) + encodeLE32Hex(0) + "00"
	if !l.rpcGotwork(types.GotWorkRequest{
		Hash:         strings.Repeat("11", 32),
		Header:       strings.Repeat("00", 80),
		CoinbaseMrkl: coinbase,
	}) {
		t.Fatal("expected chain 0 submission to be accepted")
	}
	if submitted0.Load() != 1 {
		t.Fatalf("chain 0 submissions = %d, want 1", submitted0.Load())
	}
	if submitted1.Load() != 0 {
		t.Fatalf("chain missing from template was submitted %d times", submitted1.Load())
	}
}

func TestSubmitAuxpowRequiresPayoutAndDoesNotCallGetauxblock(t *testing.T) {
	var submitCount atomic.Int64
	aux := jsonRPCServer(t, map[string]rpcReply{
		"submitauxblock": {result: true, count: &submitCount},
	})
	defer aux.Close()
	client, err := rpc.NewClient(aux.URL, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	tracker := health.NewTracker()
	l := &Listener{
		auxs:          []*rpc.Client{client},
		healthTracker: tracker,
		logger:        testLogger(),
	}
	tracker.MarkHealthy(0)

	result := l.submitAuxpow(auxSubmissionTask{
		chain:       0,
		auxHash:     strings.Repeat("11", 32),
		auxpow:      "00",
		merkleIndex: 0,
	})
	if result.accepted || result.stale {
		t.Fatalf("unexpected result: %#v", result)
	}
	if submitCount.Load() != 0 {
		t.Fatalf("submitauxblock called without payout address: %d", submitCount.Load())
	}
	if tracker.IsHealthy(0) {
		t.Fatal("missing payout should mark chain unhealthy")
	}
}

func TestSubmitAuxpowUsesSubmitauxblock(t *testing.T) {
	var submitCount atomic.Int64
	aux := jsonRPCServer(t, map[string]rpcReply{
		"submitauxblock": {result: true, count: &submitCount},
	})
	defer aux.Close()
	client, err := rpc.NewClient(aux.URL, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	l := &Listener{
		auxs:          []*rpc.Client{client},
		healthTracker: health.NewTracker(),
		logger:        testLogger(),
	}

	result := l.submitAuxpow(auxSubmissionTask{
		chain:         0,
		auxHash:       strings.Repeat("11", 32),
		auxpow:        "00",
		merkleIndex:   0,
		payoutAddress: "bbtc1qpoolpayout",
	})
	if !result.accepted {
		t.Fatalf("expected accepted submitauxblock result, got %#v", result)
	}
	if submitCount.Load() != 1 {
		t.Fatalf("submitauxblock calls = %d, want 1", submitCount.Load())
	}
}

func TestSubmitAuxpowFalseIsNotAcceptedNotStale(t *testing.T) {
	var submitCount atomic.Int64
	aux := jsonRPCServer(t, map[string]rpcReply{
		"submitauxblock": {result: false, count: &submitCount},
	})
	defer aux.Close()
	client, err := rpc.NewClient(aux.URL, testLogger())
	if err != nil {
		t.Fatal(err)
	}
	tracker := health.NewTracker()
	l := &Listener{
		auxs:          []*rpc.Client{client},
		healthTracker: tracker,
		logger:        testLogger(),
	}
	tracker.MarkHealthy(0)

	result := l.submitAuxpow(auxSubmissionTask{
		chain:         0,
		auxHash:       strings.Repeat("11", 32),
		auxpow:        "00",
		merkleIndex:   0,
		payoutAddress: "bbtc1qpoolpayout",
	})
	if result.accepted || result.stale {
		t.Fatalf("false submitauxblock should be not-accepted only, got %#v", result)
	}
	if submitCount.Load() != 1 {
		t.Fatalf("submitauxblock calls = %d, want 1", submitCount.Load())
	}
	if l.metrics.auxSubmitNotAccepted.Load() != 1 {
		t.Fatalf("not-accepted metric = %d, want 1", l.metrics.auxSubmitNotAccepted.Load())
	}
	if l.metrics.auxSubmitStale.Load() != 0 || l.metrics.auxSubmitFailed.Load() != 0 {
		t.Fatalf("unexpected stale/failed metrics: stale=%d failed=%d", l.metrics.auxSubmitStale.Load(), l.metrics.auxSubmitFailed.Load())
	}
	if !tracker.IsHealthy(0) {
		t.Fatal("not-accepted submitauxblock should keep chain healthy")
	}
}

func TestPerSolverCacheEvictsByRecordedOrder(t *testing.T) {
	l := &Listener{
		perSolverCache: make(map[string]cacheEntry),
		logger:         testLogger(),
	}

	for i := 0; i < perSolverCacheLimit+1; i++ {
		l.setPerSolverCache(fmt.Sprintf("solver-%d", i), map[string]interface{}{"i": i})
	}

	l.mu.RLock()
	_, hasFirst := l.perSolverCache["solver-0"]
	size := len(l.perSolverCache)
	l.mu.RUnlock()

	if hasFirst {
		t.Fatal("expected oldest cache entry to be evicted")
	}
	if size != perSolverCacheLimit {
		t.Fatalf("cache size = %d, want %d", size, perSolverCacheLimit)
	}
	if got := l.metrics.cacheEvictions.Load(); got != 1 {
		t.Fatalf("cache evictions = %d, want 1", got)
	}
}

func TestRPCStatusIncludesOperationalCounters(t *testing.T) {
	l := &Listener{
		auxs:           []*rpc.Client{},
		chainIDs:       []*int{},
		auxTargets:     []string{},
		healthTracker:  health.NewTracker(),
		merkleTrees:    map[string]*merkleMeta{"root": {}},
		perSolverCache: map[string]cacheEntry{"solver": {}},
		logger:         testLogger(),
	}
	l.metrics.auxTemplateBuilds.Add(2)
	l.metrics.cacheHits.Add(3)
	l.metrics.auxSubmitAccepted.Add(4)
	l.metrics.auxSubmitNotAccepted.Add(5)

	status := l.rpcStatus()
	if status["ready_count"].(int) != 0 || status["total_chains"].(int) != 0 {
		t.Fatalf("unexpected readiness payload: %#v", status)
	}
	if status["cache"].(map[string]interface{})["hits"].(int64) != 3 {
		t.Fatalf("missing cache hit counter: %#v", status["cache"])
	}
	if status["templates"].(map[string]interface{})["builds"].(int64) != 2 {
		t.Fatalf("missing template build counter: %#v", status["templates"])
	}
	if status["submissions"].(map[string]interface{})["accepted"].(int64) != 4 {
		t.Fatalf("missing submission counter: %#v", status["submissions"])
	}
	if status["submissions"].(map[string]interface{})["not_accepted"].(int64) != 5 {
		t.Fatalf("missing not-accepted counter: %#v", status["submissions"])
	}
}

func deployment(status, statusNext string) map[string]interface{} {
	bip9 := map[string]interface{}{"status": status}
	if statusNext != "" {
		bip9["status_next"] = statusNext
	}
	return map[string]interface{}{"type": "bip9", "bip9": bip9}
}

type rpcReply struct {
	result     interface{}
	errCode    int
	errMessage string
	count      *atomic.Int64
}

func jsonRPCServer(t *testing.T, replies map[string]rpcReply) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			ID     int             `json:"id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode request: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		reply, ok := replies[req.Method]
		if !ok {
			t.Errorf("unexpected method %q", req.Method)
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"result": nil,
				"error":  map[string]interface{}{"code": -32601, "message": "method not found"},
				"id":     0,
			})
			return
		}
		if reply.count != nil {
			reply.count.Add(1)
		}
		resp := map[string]interface{}{"result": reply.result, "error": nil, "id": 0}
		if reply.errMessage != "" {
			w.WriteHeader(http.StatusInternalServerError)
			resp["result"] = nil
			resp["error"] = map[string]interface{}{"code": reply.errCode, "message": reply.errMessage}
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}
