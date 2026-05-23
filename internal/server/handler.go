package server

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/blakecoin/merged-mine-proxy/internal/config"
	"github.com/blakecoin/merged-mine-proxy/internal/health"
	"github.com/blakecoin/merged-mine-proxy/internal/merkle"
	"github.com/blakecoin/merged-mine-proxy/internal/miningkey"
	"github.com/blakecoin/merged-mine-proxy/internal/rpc"
	"github.com/blakecoin/merged-mine-proxy/pkg/types"
)

const (
	auxUpdateInterval    = 15 * time.Second
	auxSolverCacheTTL    = 1 * time.Second
	deploymentCacheTTL   = 2 * time.Minute
	maxRPCBodyBytes      = 1 << 20
	perSolverCacheLimit  = 1024
	merkleTreesToKeep    = 240
	statusReportInterval = 5 * time.Minute
)

var chainAliases = []string{"MM", "MM1", "MM3", "MM4", "MM5"}
var chainDisplayNames = []string{"BlakeBitcoin", "Electron", "Lithium", "Photon", "UniversalMolecule"}

const (
	versionBitsTopMask = uint32(0xe0000000)
	versionBitsTopBits = uint32(0x20000000)
	auxpowVersionBit   = 8
	chainIDStartBit    = 16
	chainIDEndBit      = 23
	requiredTaprootBit = 2
)

type merkleMeta struct {
	Tree            []string
	Nonce           uint32
	ChainIndices    map[int]int
	AuxHashes       map[int]string
	PayoutAddresses []string
}

type cacheEntry struct {
	created time.Time
	result  map[string]interface{}
	seq     uint64
}

type cacheOrderEntry struct {
	key string
	seq uint64
}

type deploymentCacheEntry struct {
	created     time.Time
	deployments map[string]interface{}
}

type proxyMetrics struct {
	auxTemplateBuilds       atomic.Int64
	lastBuildDurationMillis atomic.Int64
	lastBuildWaitMillis     atomic.Int64
	cacheHits               atomic.Int64
	cacheMisses             atomic.Int64
	cacheEvictions          atomic.Int64
	auxSubmitAttempts       atomic.Int64
	auxSubmitAccepted       atomic.Int64
	auxSubmitNotAccepted    atomic.Int64
	auxSubmitStale          atomic.Int64
	auxSubmitFailed         atomic.Int64
}

type auxTemplateFetchResult struct {
	chain   int
	hash    string
	target  string
	chainID *int
	summary string
	ready   bool
}

type auxSubmissionTask struct {
	chain         int
	auxHash       string
	auxpow        string
	merkleIndex   int
	payoutAddress string
}

type auxSubmissionResult struct {
	chain    int
	accepted bool
	stale    bool
}

type solveChainOutcome struct {
	Label       string `json:"label"`
	Ticker      string `json:"ticker"`
	Alias       string `json:"alias"`
	Attempted   bool   `json:"attempted"`
	Accepted    bool   `json:"accepted"`
	Status      string `json:"status"`
	Hash        string `json:"hash,omitempty"`
	MerkleIndex int    `json:"merkle_index,omitempty"`
}

type solveEvent struct {
	Time          string              `json:"time"`
	Unix          int64               `json:"unix"`
	Username      string              `json:"username,omitempty"`
	ParentHash    string              `json:"parent_hash"`
	ParentStatus  string              `json:"parent_status"`
	ParentValid   bool                `json:"parent_valid"`
	AnyAccepted   bool                `json:"any_accepted"`
	AcceptedCount int                 `json:"accepted_count"`
	RejectedCount int                 `json:"rejected_count"`
	SkippedCount  int                 `json:"skipped_count"`
	Chains        []solveChainOutcome `json:"chains"`
}

// Listener is the merged-mining proxy. Eloipool asks it for coinbase aux data
// with getaux, then sends solved parent/aux candidates back with gotwork.
type Listener struct {
	parent              []*rpc.Client
	auxs                []*rpc.Client
	chainAliases        []string
	chainDisplayNames   []string
	chainIDs            []*int
	auxTargets          []string
	auxPayoutAddresses  []string
	perSolverAuxPayouts bool
	debugGotwork        bool
	healthTracker       *health.Tracker
	merkleSize          int
	rewriteTarget       string
	merkleTreeQueue     []string
	merkleTrees         map[string]*merkleMeta
	perSolverCache      map[string]cacheEntry
	perSolverCacheOrder []cacheOrderEntry
	perSolverCacheSeq   uint64
	deploymentCache     map[int]deploymentCacheEntry
	deploymentMu        sync.RWMutex
	lastVersionSummary  map[int]string
	refreshQueued       bool
	backgroundCtx       context.Context
	metrics             proxyMetrics
	mu                  sync.RWMutex
	solveMu             sync.RWMutex
	buildMu             sync.Mutex
	logger              *slog.Logger
	recentSolves        []solveEvent
}

// NewListener creates a new Listener.
func NewListener(cfg *config.Config, logger *slog.Logger) (*Listener, error) {
	if len(cfg.AuxPayoutAddresses) != len(cfg.AuxURLs) {
		return nil, fmt.Errorf("the number of aux payout addresses must match the number of aux URLs")
	}
	for i, payout := range cfg.AuxPayoutAddresses {
		if strings.TrimSpace(payout) == "" {
			return nil, fmt.Errorf("aux payout address %d must not be empty", i)
		}
	}

	parents := make([]*rpc.Client, len(cfg.ParentURLs))
	for i, url := range cfg.ParentURLs {
		c, err := rpc.NewClient(url, logger)
		if err != nil {
			return nil, fmt.Errorf("failed to create parent client %d: %w", i, err)
		}
		parents[i] = c
	}

	auxs := make([]*rpc.Client, len(cfg.AuxURLs))
	for i, url := range cfg.AuxURLs {
		c, err := rpc.NewClient(url, logger)
		if err != nil {
			return nil, fmt.Errorf("failed to create aux client %d: %w", i, err)
		}
		auxs[i] = c
	}

	payouts := make([]string, len(auxs))
	copy(payouts, cfg.AuxPayoutAddresses)
	aliases := make([]string, len(auxs))
	names := make([]string, len(auxs))
	for i := range auxs {
		aliases[i] = defaultChainAlias(i)
		names[i] = defaultChainDisplayName(i)
	}
	if len(cfg.AuxChainNames) > 0 {
		copy(names, cfg.AuxChainNames)
	}

	l := &Listener{
		parent:              parents,
		auxs:                auxs,
		chainAliases:        aliases,
		chainDisplayNames:   names,
		chainIDs:            make([]*int, len(auxs)),
		auxTargets:          make([]string, len(auxs)),
		auxPayoutAddresses:  payouts,
		perSolverAuxPayouts: cfg.PerSolverAuxPayouts,
		debugGotwork:        cfg.DebugGotwork,
		healthTracker:       health.NewTracker(),
		merkleSize:          cfg.MerkleSize,
		merkleTrees:         make(map[string]*merkleMeta),
		perSolverCache:      make(map[string]cacheEntry),
		deploymentCache:     make(map[int]deploymentCacheEntry),
		lastVersionSummary:  make(map[int]string),
		logger:              logger,
	}

	if cfg.RewriteTarget == 32 {
		l.rewriteTarget = merkle.ReverseChunks("0000000007ffffffffffffffffffffffffffffffffffffffffffffffffffffff", 2)
	} else if cfg.RewriteTarget == 100 {
		l.rewriteTarget = merkle.ReverseChunks("00000000028f5c28000000000000000000000000000000000000000000000000", 2)
	}

	return l, nil
}

// StartBackground starts periodic work after the TCP listener has bound.
func (l *Listener) StartBackground(ctx context.Context) {
	if ctx == nil {
		ctx = context.Background()
	}
	l.mu.Lock()
	l.backgroundCtx = ctx
	l.mu.Unlock()

	go l.updateAuxLoop(ctx)
	go l.statusReportLoop(ctx)
	go l.updateAuxs(ctx)
}

func (l *Listener) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRPCBodyBytes)
	defer r.Body.Close()

	var req types.JSONRPCRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		l.writeRPC(w, nil, nil, &types.JSONRPCError{Code: -32700, Message: "Parse error"})
		return
	}

	var params []json.RawMessage
	if len(req.Params) > 0 {
		if err := json.Unmarshal(req.Params, &params); err != nil {
			l.writeRPC(w, req.ID, nil, &types.JSONRPCError{Code: -32600, Message: "Invalid Request"})
			return
		}
	}

	switch req.Method {
	case "status":
		l.writeRPC(w, req.ID, l.rpcStatus(), nil)
	case "getaux":
		var selector map[string]interface{}
		if len(params) > 0 && len(params[0]) > 0 {
			_ = json.Unmarshal(params[0], &selector)
		}
		l.writeRPC(w, req.ID, l.rpcGetaux(r.Context(), selector), nil)
	case "gotwork":
		if len(params) == 0 {
			l.writeRPC(w, req.ID, nil, &types.JSONRPCError{Code: -32600, Message: "Invalid Request"})
			return
		}
		var solution types.GotWorkRequest
		if err := json.Unmarshal(params[0], &solution); err != nil {
			l.writeRPC(w, req.ID, nil, &types.JSONRPCError{Code: -32600, Message: "Invalid Request"})
			return
		}
		result := l.rpcGotwork(solution)
		l.writeRPC(w, req.ID, result, nil)
	default:
		l.writeRPC(w, req.ID, nil, &types.JSONRPCError{Code: -32601, Message: "Method not found"})
	}
}

func (l *Listener) writeRPC(w http.ResponseWriter, id interface{}, result interface{}, rpcErr *types.JSONRPCError) {
	resp := types.JSONRPCResponse{
		JSONRPC: "2.0",
		ID:      id,
		Result:  result,
		Error:   rpcErr,
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func (l *Listener) rpcGetaux(ctx context.Context, selector map[string]interface{}) map[string]interface{} {
	if ctx == nil {
		ctx = context.Background()
	}
	if len(selector) > 0 {
		// Per-solver templates allow one mining key to receive native aux
		// payouts on chains that support that address type.
		payouts := l.resolveSelectorPayouts(selector)
		cacheKey := strings.Join(payouts, "|")
		l.mu.RLock()
		cached, ok := l.perSolverCache[cacheKey]
		l.mu.RUnlock()
		if ok && time.Since(cached.created) <= auxSolverCacheTTL {
			l.metrics.cacheHits.Add(1)
			return cached.result
		}
		if ok {
			l.dropPerSolverCacheEntry(cacheKey, cached.seq)
		}
		l.metrics.cacheMisses.Add(1)
		result := l.buildAuxTemplate(ctx, payouts, false)
		l.setPerSolverCache(cacheKey, result)
		return result
	}

	l.mu.RLock()
	if len(l.merkleTreeQueue) == 0 {
		l.mu.RUnlock()
		payload := l.auxReadinessPayload()
		result := map[string]interface{}{
			"aux":   strings.Repeat("00", 40) + encodeLE32Hex(uint32(l.merkleSize)) + "00000000",
			"error": "aux chains not ready",
		}
		mergeMaps(result, payload)
		l.logger.Warn("rpc_getaux: aux chains not ready", "ready", payload["ready_count"], "total", payload["total_chains"])
		return result
	}
	merkleRoot := l.merkleTreeQueue[len(l.merkleTreeQueue)-1]
	meta := l.merkleTrees[merkleRoot]
	l.mu.RUnlock()

	// coinbaseaux is inserted into the parent coinbase. It commits to the aux
	// merkle root, merkle size, and nonce needed to prove each aux block.
	aux := merkleRoot + encodeLE32Hex(uint32(l.merkleSize)) + encodeLE32Hex(meta.Nonce)
	result := map[string]interface{}{
		"aux":         aux,
		"coinbaseaux": "fabe6d6d" + aux,
	}
	payload := l.auxReadinessPayload()
	mergeMaps(result, payload)
	if payload["ready_count"].(int) < payload["total_chains"].(int) {
		result["warning"] = "partial aux readiness"
	}
	l.addAuxTarget(result)
	return result
}

func (l *Listener) rpcGotwork(solution types.GotWorkRequest) bool {
	parentHash := solution.Hash
	parentStatus := solution.ParentStatus
	if parentStatus == "" {
		if solution.ParentValid {
			parentStatus = "parent-accepted"
		} else {
			parentStatus = "parent-rejected"
		}
	}

	coinbaseMerkle := solution.CoinbaseMrkl
	if l.debugGotwork {
		l.logger.Info("received gotwork payload debug",
			"username", solution.Username,
			"coinbase_mrkl_len", len(coinbaseMerkle)/2,
			"coinbase_mrkl_hex", coinbaseMerkle,
			"hash", solution.Hash,
			"header", solution.Header,
			"parent_status", parentStatus,
		)
	}
	pos := strings.Index(coinbaseMerkle, "fabe6d6d")
	if pos < 0 || len(coinbaseMerkle) < pos+8+80 {
		l.logger.Error("failed to find aux in coinbase")
		return false
	}
	pos += 8
	// The fabe6d6d marker is followed by the aux merkle root, tree size, and
	// nonce. The root selects the stored template snapshot used below.
	slnaux := coinbaseMerkle[pos : pos+80]
	merkleRoot := slnaux[:64]

	l.mu.RLock()
	meta := l.merkleTrees[merkleRoot]
	l.mu.RUnlock()
	if meta == nil {
		l.logger.Warn("Unknown merkle root; skipping aux submission", "root", shortHex(merkleRoot))
		return false
	}

	anySolved := false
	staleSubmission := false
	auxSolved := make([]bool, len(l.auxs))
	auxAttempted := make([]bool, len(l.auxs))
	auxHashes := make([]string, len(l.auxs))
	auxMerkleIndices := make([]int, len(l.auxs))
	tasks := make([]auxSubmissionTask, 0, len(l.auxs))

	// Build each AuxPoW payload from the immutable merkle snapshot first; the
	// network submits below can then run concurrently without sharing state.
	for chain := range l.auxs {
		if !l.healthTracker.IsHealthy(chain) {
			l.logger.Debug("Skipping unhealthy chain", "chain", l.chainAlias(chain))
			continue
		}

		chainMerkleIndex, ok := meta.ChainIndices[chain]
		if !ok {
			l.logger.Debug("Skipping chain not present in aux template", "chain", l.chainAlias(chain), "root", shortHex(merkleRoot))
			continue
		}
		if chainMerkleIndex < 0 || chainMerkleIndex >= len(meta.Tree) {
			l.logger.Warn("stale aux merkle root has no leaf; skipping share", "chain", l.chainAlias(chain), "root", shortHex(merkleRoot), "index", chainMerkleIndex)
			staleSubmission = true
			continue
		}

		branch, ok := merkle.BranchChecked(meta.Tree, chainMerkleIndex, l.merkleSize)
		if !ok {
			l.logger.Warn("stale aux merkle root has incomplete branch; skipping share", "chain", l.chainAlias(chain), "root", shortHex(merkleRoot), "index", chainMerkleIndex)
			staleSubmission = true
			continue
		}

		// AuxPoW = parent coinbase + aux merkle branch + branch index + parent
		// header. Each aux daemon verifies this proves its createauxblock hash
		// was committed in the parent coinbase.
		auxpow := coinbaseMerkle + fmt.Sprintf("%02x", len(branch))
		branchOK := true
		for _, mb := range branch {
			b, err := hex.DecodeString(mb)
			if err != nil {
				l.logger.Warn("bad merkle branch hex; skipping share", "chain", l.chainAlias(chain), "error", err)
				staleSubmission = true
				branchOK = false
				break
			}
			reverseInPlace(b)
			auxpow += hex.EncodeToString(b)
		}
		if !branchOK {
			continue
		}
		auxpow += encodeLE32Hex(uint32(chainMerkleIndex)) + solution.Header

		auxHash := meta.AuxHashes[chain]
		if auxHash == "" {
			auxHash = meta.Tree[chainMerkleIndex]
		}
		payoutAddress := ""
		if chain < len(meta.PayoutAddresses) {
			payoutAddress = meta.PayoutAddresses[chain]
		}
		auxAttempted[chain] = true
		auxHashes[chain] = auxHash
		auxMerkleIndices[chain] = chainMerkleIndex

		tasks = append(tasks, auxSubmissionTask{
			chain:         chain,
			auxHash:       auxHash,
			auxpow:        auxpow,
			merkleIndex:   chainMerkleIndex,
			payoutAddress: payoutAddress,
		})
	}

	// Submit solved aux blocks in parallel so a slow daemon cannot block every
	// other chain or the Eloipool gotwork response path.
	results := make(chan auxSubmissionResult, len(tasks))
	var wg sync.WaitGroup
	for _, task := range tasks {
		task := task
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- l.submitAuxpow(task)
		}()
	}
	wg.Wait()
	close(results)

	for result := range results {
		auxSolved[result.chain] = result.accepted
		if result.accepted {
			anySolved = true
		}
		if result.stale {
			staleSubmission = true
		}
	}

	solveFlags := make([]string, len(auxSolved))
	for i, solved := range auxSolved {
		if solved {
			solveFlags[i] = "1"
		} else {
			solveFlags[i] = "0"
		}
	}
	solveTS := time.Now().UTC().Format("2006-01-02T15:04:05.000000")
	solveFlagText := strings.Join(solveFlags, ",")
	l.logger.Info(fmt.Sprintf("%s,solve,=,%s,%s", solveTS, solveFlagText, shortHex(parentHash)))
	if anySolved || solution.ParentValid {
		l.logger.Info(fmt.Sprintf("%s,solve_status,%s,%s,%s", solveTS, parentStatus, solveFlagText, parentHash))
	}
	l.logger.Info("solve", "username", solution.Username, "parent_status", parentStatus, "aux_flags", solveFlagText, "parent_hash", parentHash)
	l.recordSolveEvent(solution, parentStatus, parentHash, auxSolved, auxAttempted, auxHashes, auxMerkleIndices)
	if anySolved || solution.ParentValid || staleSubmission {
		l.clearPerSolverCache()
		l.requestAuxRefresh()
	}
	return anySolved
}

func (l *Listener) recordSolveEvent(solution types.GotWorkRequest, parentStatus string, parentHash string, auxSolved []bool, auxAttempted []bool, auxHashes []string, auxMerkleIndices []int) {
	now := time.Now().UTC()
	event := solveEvent{
		Time:         now.Format(time.RFC3339),
		Unix:         now.Unix(),
		Username:     solution.Username,
		ParentHash:   parentHash,
		ParentStatus: parentStatus,
		ParentValid:  solution.ParentValid,
	}
	event.Chains = append(event.Chains, solveChainOutcome{
		Label:     "Blakecoin",
		Ticker:    "BLC",
		Alias:     "BLC",
		Attempted: true,
		Accepted:  solution.ParentValid,
		Status:    acceptedStatus(solution.ParentValid, true),
		Hash:      parentHash,
	})

	for chain := range l.auxs {
		attempted := chain < len(auxAttempted) && auxAttempted[chain]
		accepted := chain < len(auxSolved) && auxSolved[chain]
		outcome := solveChainOutcome{
			Label:     l.chainDisplayName(chain),
			Ticker:    tickerForChainName(l.chainDisplayName(chain), l.chainAlias(chain)),
			Alias:     l.chainAlias(chain),
			Attempted: attempted,
			Accepted:  accepted,
			Status:    acceptedStatus(accepted, attempted),
		}
		if chain < len(auxHashes) {
			outcome.Hash = auxHashes[chain]
		}
		if chain < len(auxMerkleIndices) {
			outcome.MerkleIndex = auxMerkleIndices[chain]
		}
		event.Chains = append(event.Chains, outcome)
	}

	for _, chain := range event.Chains {
		switch {
		case chain.Accepted:
			event.AcceptedCount++
			event.AnyAccepted = true
		case chain.Attempted:
			event.RejectedCount++
		default:
			event.SkippedCount++
		}
	}

	l.solveMu.Lock()
	l.recentSolves = append(l.recentSolves, event)
	if len(l.recentSolves) > 100 {
		copy(l.recentSolves, l.recentSolves[len(l.recentSolves)-100:])
		l.recentSolves = l.recentSolves[:100]
	}
	l.solveMu.Unlock()
}

func (l *Listener) recentSolveEvents() []solveEvent {
	l.solveMu.RLock()
	defer l.solveMu.RUnlock()
	out := make([]solveEvent, len(l.recentSolves))
	copy(out, l.recentSolves)
	return out
}

func acceptedStatus(accepted bool, attempted bool) string {
	if accepted {
		return "accepted"
	}
	if attempted {
		return "rejected"
	}
	return "skipped"
}

func tickerForChainName(name string, fallback string) string {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "blakecoin":
		return "BLC"
	case "blakebitcoin":
		return "BBTC"
	case "electron", "electron-elt":
		return "ELT"
	case "lithium":
		return "LIT"
	case "photon":
		return "PHO"
	case "universalmolecule", "universal molecule":
		return "UMO"
	}
	return fallback
}

func (l *Listener) submitAuxpow(task auxSubmissionTask) auxSubmissionResult {
	result := auxSubmissionResult{chain: task.chain}
	l.metrics.auxSubmitAttempts.Add(1)

	l.logger.Info(fmt.Sprintf("%s: aux_hash=%s merkle_index=%d", l.chainAlias(task.chain), task.auxHash, task.merkleIndex))
	l.logger.Info("Submitting auxpow", "chain", l.chainAlias(task.chain), "aux_hash", task.auxHash, "merkle_index", task.merkleIndex)
	if strings.TrimSpace(task.payoutAddress) == "" {
		l.logger.Error("missing aux payout address; submitauxblock requires a configured aux payout address", "chain", l.chainAlias(task.chain))
		l.metrics.auxSubmitFailed.Add(1)
		l.healthTracker.MarkUnhealthy(task.chain)
		return result
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	// submitauxblock returns true for accepted aux blocks. False is a normal
	// not-accepted result for chains that did not win this share; RPC stale
	// errors mean the daemon moved to a newer aux template first.
	raw, err := l.auxs[task.chain].Call(ctx, "submitauxblock", task.auxHash, task.auxpow)
	cancel()

	if err != nil {
		if isStaleAuxSubmissionError(err) {
			l.logger.Info("aux chain moved past hash; share orphaned at aux layer", "chain", l.chainAlias(task.chain), "aux_hash", task.auxHash)
			l.metrics.auxSubmitStale.Add(1)
			l.healthTracker.MarkHealthy(task.chain)
			result.stale = true
			return result
		}
		l.logger.Error("submission failed; marking chain unhealthy", "chain", l.chainAlias(task.chain), "error", err)
		l.metrics.auxSubmitFailed.Add(1)
		l.healthTracker.MarkUnhealthy(task.chain)
		return result
	}

	var accepted bool
	if err := json.Unmarshal(raw, &accepted); err != nil {
		l.logger.Error("invalid submit result; marking chain unhealthy", "chain", l.chainAlias(task.chain), "error", err)
		l.metrics.auxSubmitFailed.Add(1)
		l.healthTracker.MarkUnhealthy(task.chain)
		return result
	}

	result.accepted = accepted
	if accepted {
		l.logger.Info(fmt.Sprintf("%s: Block accepted!", l.chainAlias(task.chain)))
		l.logger.Info("Block accepted", "chain", l.chainAlias(task.chain))
		l.metrics.auxSubmitAccepted.Add(1)
		l.healthTracker.MarkHealthy(task.chain)
		return result
	}

	l.logger.Info("aux daemon returned not-accepted for this share", "chain", l.chainAlias(task.chain), "aux_hash", task.auxHash)
	l.metrics.auxSubmitNotAccepted.Add(1)
	l.healthTracker.MarkHealthy(task.chain)
	return result
}

func (l *Listener) updateAuxLoop(ctx context.Context) {
	ticker := time.NewTicker(auxUpdateInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.updateAuxs(ctx)
		}
	}
}

func (l *Listener) updateAuxs(ctx context.Context) {
	result := l.buildAuxTemplate(ctx, nil, true)
	if root, _ := result["merkle_root"].(string); root != "" {
		l.logger.Debug("Merkle tree updated", "root", shortHex(root))
	}
}

func (l *Listener) fetchAuxTemplate(ctx context.Context, chain int, payout string, globalTemplate bool) auxTemplateFetchResult {
	if ctx == nil {
		ctx = context.Background()
	}
	result := auxTemplateFetchResult{chain: chain}
	started := time.Now()
	defer func() {
		l.logger.Debug("aux template fetch completed",
			"chain", l.chainAlias(chain),
			"duration_ms", time.Since(started).Milliseconds(),
			"ready", result.ready,
		)
	}()

	if payout == "" {
		l.logger.Error("no aux payout address configured; skipping aux template fetch", "chain", l.chainAlias(chain))
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}

	callCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	// createauxblock reserves a chain-specific block candidate and returns the
	// aux hash/target that must be committed into the parent coinbase.
	raw, err := l.auxs[chain].Call(callCtx, "createauxblock", payout)
	cancel()
	if err != nil {
		if isStaleAuxSubmissionError(err) {
			l.logger.Info("aux template fetch saw stale state; refreshing", "chain", l.chainAlias(chain), "error", err)
			if globalTemplate {
				l.healthTracker.MarkHealthy(chain)
			}
			return result
		}
		l.logger.Error("Failed to get aux block", "chain", l.chainAlias(chain), "error", err)
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}

	var auxBlock types.AuxBlock
	if err := json.Unmarshal(raw, &auxBlock); err != nil || auxBlock.Hash == "" || auxBlock.Target == "" {
		l.logger.Warn("Invalid aux_block response", "chain", l.chainAlias(chain))
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}
	auxBlock.Hash = strings.ToLower(auxBlock.Hash)
	auxBlock.Target = strings.ToLower(auxBlock.Target)
	if !validHexLen(auxBlock.Hash, 64) || !validHexLen(auxBlock.Target, 64) {
		l.logger.Warn("Invalid aux_block hash/target hex", "chain", l.chainAlias(chain), "hash", shortHex(auxBlock.Hash), "target", shortHex(auxBlock.Target))
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}

	deployments, err := l.fetchAuxDeployments(ctx, chain)
	if err != nil {
		l.logger.Error("Failed to fetch aux deployments", "chain", l.chainAlias(chain), "error", err)
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}
	if failures := requiredBIP9SignalFailures(auxBlock, deployments); len(failures) > 0 {
		l.logger.Error("refusing aux template with bad future-BIP signalling", "chain", l.chainAlias(chain), "failures", strings.Join(failures, "; "))
		if globalTemplate {
			l.healthTracker.MarkUnhealthy(chain)
		}
		return result
	}

	chainID := chain
	if auxBlock.ChainID != nil {
		chainID = *auxBlock.ChainID
	}
	id := chainID
	result.hash = auxBlock.Hash
	result.target = merkle.ReverseChunks(auxBlock.Target, 2)
	result.chainID = &id
	result.summary = summarizeAuxVersionBits(auxBlock, chainID)
	result.ready = true
	if globalTemplate {
		l.healthTracker.MarkHealthy(chain)
	}
	return result
}

func (l *Listener) buildAuxTemplate(ctx context.Context, payoutAddresses []string, propagateParent bool) map[string]interface{} {
	if ctx == nil {
		ctx = context.Background()
	}
	started := time.Now()
	globalTemplate := payoutAddresses == nil
	auxBlockHashes := make(map[int]string)
	lockWait := time.Duration(0)

	payouts := l.normalizePayoutAddresses(payoutAddresses)
	merkleLeaves := make([]string, l.merkleSize)
	for i := 0; i < l.merkleSize; i++ {
		merkleLeaves[i] = fmt.Sprintf("%062x%02x", 0, i)
	}

	fetchedChainIDs := make([]*int, len(l.auxs))
	fetchedTargets := make([]string, len(l.auxs))
	fetchedSummaries := make(map[int]string)
	healthyCount := 0

	// Fetch each aux daemon in parallel before taking buildMu so one slow
	// daemon cannot hold up unrelated template work.
	fetchResults := make(chan auxTemplateFetchResult, len(l.auxs))
	var wg sync.WaitGroup
	for chain := range l.auxs {
		chain := chain
		wg.Add(1)
		go func() {
			defer wg.Done()
			fetchResults <- l.fetchAuxTemplate(ctx, chain, payouts[chain], globalTemplate)
		}()
	}
	wg.Wait()
	close(fetchResults)

	for fetched := range fetchResults {
		if !fetched.ready {
			continue
		}
		fetchedChainIDs[fetched.chain] = fetched.chainID
		auxBlockHashes[fetched.chain] = fetched.hash
		fetchedTargets[fetched.chain] = fetched.target
		if fetched.summary != "" {
			fetchedSummaries[fetched.chain] = fetched.summary
		}
		healthyCount++
	}

	// Serialize merkle state updates and parent propagation so setworkaux calls
	// are delivered in the same order as committed merkle roots.
	lockStarted := time.Now()
	l.buildMu.Lock()
	lockWait = time.Since(lockStarted)
	defer func() {
		l.buildMu.Unlock()
		total := time.Since(started)
		l.metrics.auxTemplateBuilds.Add(1)
		l.metrics.lastBuildDurationMillis.Store(total.Milliseconds())
		l.metrics.lastBuildWaitMillis.Store(lockWait.Milliseconds())
		l.logger.Debug("buildAuxTemplate completed",
			"duration_ms", total.Milliseconds(),
			"lock_wait_ms", lockWait.Milliseconds(),
			"global", globalTemplate,
			"chains", len(auxBlockHashes),
			"per_solver", !globalTemplate,
		)
	}()

	if healthyCount < len(l.auxs) {
		l.logger.Warn("Only some aux chains healthy", "healthy", healthyCount, "total", len(l.auxs))
	}
	if globalTemplate {
		l.commitAuxTemplateState(fetchedChainIDs, fetchedTargets, fetchedSummaries)
	}

	if len(auxBlockHashes) == 0 {
		result := map[string]interface{}{"aux": strings.Repeat("00", 40) + encodeLE32Hex(uint32(l.merkleSize)) + "00000000", "error": "aux chains not ready"}
		mergeMaps(result, l.auxReadinessPayload())
		return result
	}

	activeChainIDs := make([]*int, len(l.auxs))
	for chain := range l.auxs {
		if _, ok := auxBlockHashes[chain]; ok {
			activeChainIDs[chain] = fetchedChainIDs[chain]
		}
	}
	nonce, err := l.chooseMerkleNonce(activeChainIDs)
	if err != nil {
		result := map[string]interface{}{"aux": strings.Repeat("00", 40) + encodeLE32Hex(uint32(l.merkleSize)) + "00000000", "error": err.Error()}
		mergeMaps(result, l.auxReadinessPayload())
		return result
	}

	chainIndices := make(map[int]int)
	for chain, auxHash := range auxBlockHashes {
		if fetchedChainIDs[chain] == nil {
			continue
		}
		idx := l.calcMerkleIndexFor(*fetchedChainIDs[chain], nonce)
		chainIndices[chain] = idx
		merkleLeaves[idx] = auxHash
	}

	// The aux merkle tree stores one aux hash per active chain. Chain IDs and
	// nonce choose deterministic leaf positions so merged-mined chains do not
	// collide in the same tree.
	mt, err := merkle.NewTreeChecked(merkleLeaves)
	if err != nil {
		result := map[string]interface{}{"error": err.Error()}
		mergeMaps(result, l.auxReadinessPayload())
		return result
	}
	merkleTree := mt.Detail()
	if len(merkleTree) == 0 {
		result := map[string]interface{}{"error": "empty merkle tree"}
		mergeMaps(result, l.auxReadinessPayload())
		return result
	}
	merkleRoot := merkleTree[len(merkleTree)-1]
	mmaux := merkleRoot + encodeLE32Hex(uint32(l.merkleSize)) + encodeLE32Hex(nonce)

	l.mu.Lock()
	if _, exists := l.merkleTrees[merkleRoot]; !exists {
		l.merkleTrees[merkleRoot] = &merkleMeta{
			Tree:            merkleTree,
			Nonce:           nonce,
			ChainIndices:    chainIndices,
			AuxHashes:       auxBlockHashes,
			PayoutAddresses: append([]string(nil), payouts...),
		}
		l.merkleTreeQueue = append(l.merkleTreeQueue, merkleRoot)
		if len(l.merkleTreeQueue) > merkleTreesToKeep {
			old := l.merkleTreeQueue[0]
			l.merkleTreeQueue = l.merkleTreeQueue[1:]
			delete(l.merkleTrees, old)
		}
	}
	if propagateParent {
		l.perSolverCache = make(map[string]cacheEntry)
		l.perSolverCacheOrder = nil
	}
	l.mu.Unlock()

	if propagateParent {
		coinbaseAux := "fabe6d6d" + mmaux
		for _, p := range l.parent {
			callCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
			// setworkaux tells the parent daemon which aux commitment must be
			// included in future getblocktemplate coinbases.
			_, err := p.Call(callCtx, "setworkaux", "MM", coinbaseAux)
			cancel()
			if err != nil {
				l.logger.Error("setworkaux failed", "parent", p.Host(), "error", err)
			}
		}
	}

	result := map[string]interface{}{
		"aux":              mmaux,
		"coinbaseaux":      "fabe6d6d" + mmaux,
		"merkle_root":      merkleRoot,
		"payout_addresses": payouts,
	}
	payload := l.auxReadinessPayload()
	mergeMaps(result, payload)
	if payload["ready_count"].(int) <= 0 && payload["total_chains"].(int) > 0 {
		result["error"] = "aux chains not ready"
	} else if payload["ready_count"].(int) < payload["total_chains"].(int) {
		result["warning"] = "partial aux readiness"
	}
	if !globalTemplate && len(auxBlockHashes) < len(l.auxs) {
		result["warning"] = "partial per-solver aux template"
	}
	if globalTemplate {
		l.addAuxTarget(result)
	} else {
		l.addAuxTargetFromTargets(result, fetchedTargets)
	}
	return result
}

func (l *Listener) resolveSelectorPayouts(selector map[string]interface{}) []string {
	username, _ := selector["username"].(string)
	if username == "" {
		username, _ = selector["stratum_username"].(string)
	}
	if raw, ok := selector["payout_addresses"]; ok {
		if arr, ok := raw.([]interface{}); ok {
			payouts := make([]string, len(l.auxs))
			for i := range payouts {
				if i < len(arr) && arr[i] != nil {
					payouts[i] = strings.TrimSpace(fmt.Sprint(arr[i]))
				}
			}
			return l.normalizePayoutAddresses(payouts)
		}
	}

	payouts := l.normalizePayoutAddresses(nil)
	if !l.perSolverAuxPayouts || username == "" {
		return payouts
	}
	for i, base := range payouts {
		if derived, ok := miningkey.ResolveV2Payout(username, base); ok {
			payouts[i] = derived
		}
	}
	return payouts
}

func (l *Listener) normalizePayoutAddresses(input []string) []string {
	out := make([]string, len(l.auxs))
	for i := range out {
		if i < len(input) {
			out[i] = strings.TrimSpace(input[i])
		}
		if out[i] == "" && i < len(l.auxPayoutAddresses) {
			out[i] = strings.TrimSpace(l.auxPayoutAddresses[i])
		}
	}
	return out
}

func (l *Listener) chainID(chain int) (int, bool) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if chain < 0 || chain >= len(l.chainIDs) || l.chainIDs[chain] == nil {
		return 0, false
	}
	return *l.chainIDs[chain], true
}

func (l *Listener) auxStateSnapshot() ([]*int, []string) {
	l.mu.RLock()
	defer l.mu.RUnlock()

	chainIDs := make([]*int, len(l.chainIDs))
	for i, id := range l.chainIDs {
		if id != nil {
			v := *id
			chainIDs[i] = &v
		}
	}
	targets := append([]string(nil), l.auxTargets...)
	return chainIDs, targets
}

func (l *Listener) commitAuxTemplateState(chainIDs []*int, targets []string, summaries map[int]string) {
	type versionLog struct {
		chain   int
		summary string
	}
	logs := []versionLog{}

	l.mu.Lock()
	for chain, id := range chainIDs {
		if id == nil {
			continue
		}
		v := *id
		l.chainIDs[chain] = &v
	}
	for chain, target := range targets {
		if target != "" {
			l.auxTargets[chain] = target
		}
	}
	for chain, summary := range summaries {
		if summary != "" && l.lastVersionSummary[chain] != summary {
			l.lastVersionSummary[chain] = summary
			logs = append(logs, versionLog{chain: chain, summary: summary})
		}
	}
	l.mu.Unlock()

	for _, entry := range logs {
		l.logger.Info("aux template", "chain", l.chainAlias(entry.chain), "summary", entry.summary)
	}
}

func (l *Listener) auxReadinessPayload() map[string]interface{} {
	chainIDs, auxTargets := l.auxStateSnapshot()
	readiness := make([]map[string]interface{}, 0, len(l.auxs))
	readyRows := make([]map[string]interface{}, 0, len(l.auxs))
	waitingRows := make([]map[string]interface{}, 0, len(l.auxs))
	for i := range l.auxs {
		state := l.healthTracker.Snapshot(i)
		var chainID interface{}
		if chainIDs[i] != nil {
			chainID = *chainIDs[i]
		}
		healthy := l.healthTracker.IsHealthy(i)
		ready := healthy && chainIDs[i] != nil && auxTargets[i] != ""
		status := "syncing"
		if ready {
			status = "ready"
		} else if !healthy {
			status = "unhealthy"
		}
		row := map[string]interface{}{
			"chain_idx": i,
			"alias":     l.chainAlias(i),
			"chain":     l.chainDisplayName(i),
			"chain_id":  chainID,
			"ready":     ready,
			"status":    status,
			"failures":  state.Failures,
		}
		readiness = append(readiness, row)
		if ready {
			readyRows = append(readyRows, row)
		} else {
			waitingRows = append(waitingRows, row)
		}
	}
	return map[string]interface{}{
		"ready_count":    len(readyRows),
		"total_chains":   len(readiness),
		"ready_chains":   readyRows,
		"waiting_chains": waitingRows,
		"readiness":      readiness,
	}
}

func (l *Listener) rpcStatus() map[string]interface{} {
	// JSON-RPC status is intentionally lightweight so dashboards and deploy
	// checks can inspect readiness without scraping log text.
	status := l.auxReadinessPayload()

	l.mu.RLock()
	cacheSize := len(l.perSolverCache)
	merkleRoots := len(l.merkleTrees)
	refreshQueued := l.refreshQueued
	l.mu.RUnlock()

	parents := make([]map[string]interface{}, 0, len(l.parent))
	for i, p := range l.parent {
		total, failures, idle := p.Stats()
		parents = append(parents, map[string]interface{}{
			"index":    i,
			"host":     p.Host(),
			"requests": total,
			"failures": failures,
			"idle":     idle,
		})
	}

	auxRPC := make([]map[string]interface{}, 0, len(l.auxs))
	for i, aux := range l.auxs {
		total, failures, idle := aux.Stats()
		auxRPC = append(auxRPC, map[string]interface{}{
			"index":    i,
			"alias":    l.chainAlias(i),
			"host":     aux.Host(),
			"requests": total,
			"failures": failures,
			"idle":     idle,
		})
	}

	status["cache"] = map[string]interface{}{
		"per_solver_entries": cacheSize,
		"evictions":          l.metrics.cacheEvictions.Load(),
		"hits":               l.metrics.cacheHits.Load(),
		"misses":             l.metrics.cacheMisses.Load(),
	}
	status["templates"] = map[string]interface{}{
		"builds":                 l.metrics.auxTemplateBuilds.Load(),
		"last_duration_ms":       l.metrics.lastBuildDurationMillis.Load(),
		"last_build_wait_ms":     l.metrics.lastBuildWaitMillis.Load(),
		"stored_merkle_roots":    merkleRoots,
		"refresh_queued":         refreshQueued,
		"refresh_interval_secs":  int(auxUpdateInterval.Seconds()),
		"stored_merkle_capacity": merkleTreesToKeep,
	}
	status["submissions"] = map[string]interface{}{
		"attempts":     l.metrics.auxSubmitAttempts.Load(),
		"accepted":     l.metrics.auxSubmitAccepted.Load(),
		"not_accepted": l.metrics.auxSubmitNotAccepted.Load(),
		"stale":        l.metrics.auxSubmitStale.Load(),
		"failed":       l.metrics.auxSubmitFailed.Load(),
	}
	status["recent_solves"] = l.recentSolveEvents()
	status["parents"] = parents
	status["aux_rpc"] = auxRPC
	return status
}

func (l *Listener) addAuxTarget(result map[string]interface{}) {
	_, auxTargets := l.auxStateSnapshot()
	l.addAuxTargetFromTargets(result, auxTargets)
}

func (l *Listener) addAuxTargetFromTargets(result map[string]interface{}, auxTargets []string) {
	if l.rewriteTarget != "" {
		result["aux_target"] = l.rewriteTarget
		return
	}
	targets := make([]string, 0, len(auxTargets))
	for _, target := range auxTargets {
		if target != "" {
			targets = append(targets, target)
		}
	}
	if len(targets) == 0 {
		return
	}
	sort.Strings(targets)
	result["aux_target"] = merkle.ReverseChunks(targets[len(targets)-1], 2)
}

func (l *Listener) clearPerSolverCache() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.perSolverCache = make(map[string]cacheEntry)
	l.perSolverCacheOrder = nil
}

func (l *Listener) setPerSolverCache(key string, result map[string]interface{}) {
	l.mu.Lock()
	defer l.mu.Unlock()

	_, existed := l.perSolverCache[key]
	if !existed {
		l.evictPerSolverCacheLocked()
	}
	l.perSolverCacheSeq++
	seq := l.perSolverCacheSeq
	l.perSolverCache[key] = cacheEntry{created: time.Now(), result: result, seq: seq}
	l.perSolverCacheOrder = append(l.perSolverCacheOrder, cacheOrderEntry{key: key, seq: seq})
	if len(l.perSolverCacheOrder) > perSolverCacheLimit*4 {
		l.compactPerSolverCacheOrderLocked()
	}
}

func (l *Listener) dropPerSolverCacheEntry(key string, seq uint64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if current, ok := l.perSolverCache[key]; ok && current.seq == seq {
		delete(l.perSolverCache, key)
		l.metrics.cacheEvictions.Add(1)
	}
}

func (l *Listener) evictPerSolverCacheLocked() {
	for len(l.perSolverCache) >= perSolverCacheLimit && len(l.perSolverCacheOrder) > 0 {
		entry := l.perSolverCacheOrder[0]
		l.perSolverCacheOrder[0] = cacheOrderEntry{}
		l.perSolverCacheOrder = l.perSolverCacheOrder[1:]
		if current, ok := l.perSolverCache[entry.key]; ok && current.seq == entry.seq {
			delete(l.perSolverCache, entry.key)
			l.metrics.cacheEvictions.Add(1)
			return
		}
	}
	if len(l.perSolverCache) >= perSolverCacheLimit {
		for key := range l.perSolverCache {
			delete(l.perSolverCache, key)
			l.metrics.cacheEvictions.Add(1)
			return
		}
	}
}

func (l *Listener) compactPerSolverCacheOrderLocked() {
	// Keep the cache order queue bounded even when a hot solver refreshes the
	// same key repeatedly and leaves older sequence entries behind.
	compacted := l.perSolverCacheOrder[:0]
	for _, entry := range l.perSolverCacheOrder {
		if current, ok := l.perSolverCache[entry.key]; ok && current.seq == entry.seq {
			compacted = append(compacted, entry)
		}
	}
	l.perSolverCacheOrder = compacted
}

func (l *Listener) requestAuxRefresh() {
	l.mu.Lock()
	if l.refreshQueued {
		l.mu.Unlock()
		return
	}
	l.refreshQueued = true
	l.mu.Unlock()

	ctx := l.backgroundContext()
	go func() {
		defer func() {
			l.mu.Lock()
			l.refreshQueued = false
			l.mu.Unlock()
		}()

		select {
		case <-ctx.Done():
			return
		default:
		}
		l.updateAuxs(ctx)
	}()
}

func (l *Listener) backgroundContext() context.Context {
	l.mu.RLock()
	ctx := l.backgroundCtx
	l.mu.RUnlock()
	if ctx == nil {
		return context.Background()
	}
	return ctx
}

func (l *Listener) fetchAuxDeployments(ctx context.Context, chain int) (map[string]interface{}, error) {
	l.deploymentMu.RLock()
	cached, ok := l.deploymentCache[chain]
	l.deploymentMu.RUnlock()
	if ok && time.Since(cached.created) <= deploymentCacheTTL {
		return copyStringInterfaceMap(cached.deployments), nil
	}

	deployments, err := l.fetchAuxDeploymentsLive(ctx, chain)
	if err != nil {
		return nil, err
	}
	l.deploymentMu.Lock()
	if l.deploymentCache == nil {
		l.deploymentCache = make(map[int]deploymentCacheEntry)
	}
	l.deploymentCache[chain] = deploymentCacheEntry{
		created:     time.Now(),
		deployments: copyStringInterfaceMap(deployments),
	}
	l.deploymentMu.Unlock()
	return deployments, nil
}

func (l *Listener) fetchAuxDeploymentsLive(ctx context.Context, chain int) (map[string]interface{}, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	callCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	raw, err := l.auxs[chain].Call(callCtx, "getdeploymentinfo")
	cancel()
	if err == nil {
		var info map[string]interface{}
		if json.Unmarshal(raw, &info) == nil {
			if deployments, ok := info["deployments"].(map[string]interface{}); ok {
				return deployments, nil
			}
		}
	} else {
		var rpcErr *rpc.RPCError
		if !errors.As(err, &rpcErr) || rpcErr.Code != -32601 {
			return nil, err
		}
	}

	callCtx, cancel = context.WithTimeout(ctx, 30*time.Second)
	raw, err = l.auxs[chain].Call(callCtx, "getblockchaininfo")
	cancel()
	if err != nil {
		return nil, err
	}
	var info map[string]interface{}
	if err := json.Unmarshal(raw, &info); err != nil {
		return nil, err
	}
	if softforks, ok := info["softforks"].(map[string]interface{}); ok {
		return softforks, nil
	}
	if bip9, ok := info["bip9_softforks"].(map[string]interface{}); ok {
		converted := make(map[string]interface{}, len(bip9))
		for name, v := range bip9 {
			converted[name] = map[string]interface{}{"type": "bip9", "bip9": v}
		}
		return converted, nil
	}
	return map[string]interface{}{}, nil
}

func copyStringInterfaceMap(in map[string]interface{}) map[string]interface{} {
	out := make(map[string]interface{}, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func requiredBIP9SignalFailures(auxBlock types.AuxBlock, deployments map[string]interface{}) []string {
	failures := []string{}
	state := deploymentTemplateState(deployments["taproot"])
	if state != "started" && state != "locked_in" {
		return failures
	}
	version, versionHex, ok := auxTemplateVersion(auxBlock)
	if !ok {
		return append(failures, fmt.Sprintf("taproot is %s but aux template has no version/versionHex", state))
	}
	if version&(1<<requiredTaprootBit) == 0 {
		return append(failures, fmt.Sprintf("taproot is %s but aux template version %s does not signal bit %d", state, versionHex, requiredTaprootBit))
	}
	return failures
}

func deploymentTemplateState(deployment interface{}) string {
	obj, ok := deployment.(map[string]interface{})
	if !ok {
		return ""
	}
	bip9, ok := obj["bip9"].(map[string]interface{})
	if !ok {
		return ""
	}
	if v, ok := bip9["status_next"].(string); ok {
		return strings.ToLower(v)
	}
	if v, ok := bip9["status"].(string); ok {
		return strings.ToLower(v)
	}
	return ""
}

func auxTemplateVersion(auxBlock types.AuxBlock) (uint32, string, bool) {
	if auxBlock.Version != nil {
		switch v := auxBlock.Version.(type) {
		case float64:
			return uint32(v), fmt.Sprintf("%08x", uint32(v)), true
		case int:
			return uint32(v), fmt.Sprintf("%08x", uint32(v)), true
		case string:
			if parsed, err := strconv.ParseUint(v, 0, 32); err == nil {
				return uint32(parsed), fmt.Sprintf("%08x", uint32(parsed)), true
			}
		}
	}
	if auxBlock.VersionHex != "" {
		parsed, err := strconv.ParseUint(auxBlock.VersionHex, 16, 32)
		if err == nil {
			return uint32(parsed), strings.ToLower(auxBlock.VersionHex), true
		}
	}
	return 0, "", false
}

func summarizeAuxVersionBits(auxBlock types.AuxBlock, defaultChainID int) string {
	version, versionHex, ok := auxTemplateVersion(auxBlock)
	if !ok {
		return ""
	}
	chainID := defaultChainID
	if auxBlock.ChainID != nil {
		chainID = *auxBlock.ChainID
	} else {
		chainID = int((version >> chainIDStartBit) & 0xff)
	}
	topMode := "legacy"
	if version&versionBitsTopMask == versionBitsTopBits {
		topMode = "bip9"
	}
	auxpowEnabled := "no"
	if version&(1<<auxpowVersionBit) != 0 {
		auxpowEnabled = "yes"
	}
	signals := []string{}
	if topMode == "bip9" {
		names := map[int]string{0: "csv", 1: "segwit", 2: "taproot"}
		for bit := 0; bit < 29; bit++ {
			if bit == auxpowVersionBit || (bit >= chainIDStartBit && bit <= chainIDEndBit) || bit == 4 {
				continue
			}
			if version&(1<<bit) != 0 {
				name, ok := names[bit]
				if !ok {
					name = fmt.Sprintf("bit%d", bit)
				}
				signals = append(signals, name)
			}
		}
	}
	signalText := "none"
	if len(signals) > 0 {
		signalText = strings.Join(signals, ",")
	}
	return fmt.Sprintf("version=%s top=%s auxpow=%s chain_id=%d signals=%s", versionHex, topMode, auxpowEnabled, chainID, signalText)
}

func (l *Listener) chooseMerkleNonce(chainIDs []*int) (uint32, error) {
	active := []int{}
	for _, id := range chainIDs {
		if id != nil {
			active = append(active, *id)
		}
	}
	if len(active) <= 1 {
		return 0, nil
	}
	for nonce := uint32(0); nonce < 1<<16; nonce++ {
		used := make(map[int]bool, len(active))
		collision := false
		for _, chainID := range active {
			idx := l.calcMerkleIndexFor(chainID, nonce)
			if used[idx] {
				collision = true
				break
			}
			used[idx] = true
		}
		if !collision {
			return nonce, nil
		}
	}
	return 0, fmt.Errorf("unable to find collision-free aux merkle nonce")
}

func (l *Listener) calcMerkleIndexFor(chainID int, nonce uint32) int {
	rand := nonce
	rand = (rand*1103515245 + 12345) & 0xffffffff
	rand += uint32(chainID)
	rand = (rand*1103515245 + 12345) & 0xffffffff
	return int(rand % uint32(l.merkleSize))
}

func isStaleAuxSubmissionError(err error) bool {
	var rpcErr *rpc.RPCError
	if !errors.As(err, &rpcErr) {
		return false
	}
	msg := strings.ToLower(rpcErr.Message)
	staleKeywords := []string{
		"block hash unknown",
		"block-not-found",
		"block not found",
		"hash not found",
		"unknown block",
		"not in main chain",
		"not on best chain",
		"inconclusive",
		"stale",
		"orphan",
	}
	matches := false
	for _, keyword := range staleKeywords {
		if strings.Contains(msg, keyword) {
			matches = true
			break
		}
	}
	if !matches {
		return false
	}
	return rpcErr.Code == -8 || rpcErr.Code == -1 || rpcErr.Code == -32603
}

func (l *Listener) statusReportLoop(ctx context.Context) {
	ticker := time.NewTicker(statusReportInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.logStatusReport()
		}
	}
}

func (l *Listener) logStatusReport() {
	chainIDs, _ := l.auxStateSnapshot()
	l.mu.RLock()
	cacheSize := len(l.perSolverCache)
	merkleRoots := len(l.merkleTrees)
	l.mu.RUnlock()
	l.logger.Info("Status: Proxy",
		"template_builds", l.metrics.auxTemplateBuilds.Load(),
		"last_build_ms", l.metrics.lastBuildDurationMillis.Load(),
		"last_build_wait_ms", l.metrics.lastBuildWaitMillis.Load(),
		"cache_entries", cacheSize,
		"cache_hits", l.metrics.cacheHits.Load(),
		"cache_misses", l.metrics.cacheMisses.Load(),
		"cache_evictions", l.metrics.cacheEvictions.Load(),
		"stored_merkle_roots", merkleRoots,
		"submit_attempts", l.metrics.auxSubmitAttempts.Load(),
		"submit_accepted", l.metrics.auxSubmitAccepted.Load(),
		"submit_not_accepted", l.metrics.auxSubmitNotAccepted.Load(),
		"submit_stale", l.metrics.auxSubmitStale.Load(),
		"submit_failed", l.metrics.auxSubmitFailed.Load(),
	)
	for i, p := range l.parent {
		total, failures, idle := p.Stats()
		l.logger.Info("Status: Parent", "index", i, "host", p.Host(), "requests", total, "failures", failures, "idle", idle)
	}
	for i, aux := range l.auxs {
		total, failures, idle := aux.Stats()
		healthy := l.healthTracker.IsHealthy(i)
		chainID := "N/A"
		if chainIDs[i] != nil {
			chainID = strconv.Itoa(*chainIDs[i])
		}
		l.logger.Info("Status: Aux", "alias", l.chainAlias(i), "chain_id", chainID, "host", aux.Host(), "requests", total, "failures", failures, "idle", idle, "healthy", healthy)
	}
}

func validHexLen(s string, n int) bool {
	if len(s) != n {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

func (l *Listener) chainAlias(i int) string {
	if l != nil && i >= 0 && i < len(l.chainAliases) && strings.TrimSpace(l.chainAliases[i]) != "" {
		return l.chainAliases[i]
	}
	return defaultChainAlias(i)
}

func defaultChainAlias(i int) string {
	if i >= 0 && i < len(chainAliases) {
		return chainAliases[i]
	}
	return fmt.Sprintf("MM%d", i)
}

func (l *Listener) chainDisplayName(i int) string {
	if l != nil && i >= 0 && i < len(l.chainDisplayNames) && strings.TrimSpace(l.chainDisplayNames[i]) != "" {
		return l.chainDisplayNames[i]
	}
	return defaultChainDisplayName(i)
}

func defaultChainDisplayName(i int) string {
	if i >= 0 && i < len(chainDisplayNames) {
		return chainDisplayNames[i]
	}
	return defaultChainAlias(i)
}

func encodeLE32Hex(value uint32) string {
	return fmt.Sprintf("%02x%02x%02x%02x", byte(value), byte(value>>8), byte(value>>16), byte(value>>24))
}

func reverseInPlace(b []byte) {
	for i, j := 0, len(b)-1; i < j; i, j = i+1, j-1 {
		b[i], b[j] = b[j], b[i]
	}
}

func mergeMaps(dst, src map[string]interface{}) {
	for k, v := range src {
		dst[k] = v
	}
}

func shortHex(s string) string {
	if len(s) <= 16 {
		return s
	}
	return s[:16]
}
