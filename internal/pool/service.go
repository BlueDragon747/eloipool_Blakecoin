package pool

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/SidGrip/eliopool-15.21-go/internal/block"
	proxyconfig "github.com/SidGrip/eliopool-15.21-go/internal/config"
	"github.com/SidGrip/eliopool-15.21-go/internal/hash"
	"github.com/SidGrip/eliopool-15.21-go/internal/merkle"
	"github.com/SidGrip/eliopool-15.21-go/internal/rpc"
	"github.com/SidGrip/eliopool-15.21-go/internal/server"
	"github.com/SidGrip/eliopool-15.21-go/internal/share"
	"github.com/SidGrip/eliopool-15.21-go/internal/stratum"
	"github.com/SidGrip/eliopool-15.21-go/internal/work"
	"github.com/SidGrip/eliopool-15.21-go/pkg/types"
)

const recentAuxGrace = 30 * time.Second

type Service struct {
	cfg       *Config
	logger    *slog.Logger
	parent    *rpc.Client
	proxy     *rpc.Client
	auxRPCs   []*rpc.Client
	work      *work.Manager
	shareFile *os.File
	baseDiff  int

	minersMu sync.RWMutex
	miners   map[string]minerState

	jobsMu sync.RWMutex
	jobs   map[string]*work.Job

	auxMu          sync.RWMutex
	lastAux        string
	lastAuxAt      time.Time
	lastAuxTarget  *big.Int
	accepted       atomic.Int64
	rejected       atomic.Int64
	parentFound    atomic.Int64
	gotworkSent    atomic.Int64
	gotworkSkipped atomic.Int64
}

type minerState struct {
	Username string
	Remote   string
	LastSeen time.Time
	Shares   int64
	Blocks   int64
}

func NewService(cfg *Config, logger *slog.Logger) (*Service, error) {
	if logger == nil {
		logger = slog.Default()
	}
	parent, err := rpc.NewClient(cfg.ParentRPCURL, logger)
	if err != nil {
		return nil, fmt.Errorf("parent rpc: %w", err)
	}
	proxyURL := "http://" + cfg.ProxyAddr + "/"
	proxy, err := rpc.NewClient(proxyURL, logger)
	if err != nil {
		return nil, fmt.Errorf("proxy rpc: %w", err)
	}
	wm := work.NewManager(parent)
	baseDifficulty := cfg.BaseDifficulty
	if baseDifficulty <= 0 {
		baseDifficulty = 1
	}
	if err := wm.SetShareDifficulty(baseDifficulty); err != nil {
		return nil, fmt.Errorf("share difficulty: %w", err)
	}
	script, err := payoutScript(cfg)
	if err != nil {
		return nil, err
	}
	wm.SetPayoutScript(script)
	s := &Service{
		cfg:      cfg,
		logger:   logger,
		parent:   parent,
		proxy:    proxy,
		work:     wm,
		baseDiff: baseDifficulty,
		miners:   make(map[string]minerState),
		jobs:     make(map[string]*work.Job),
	}
	for _, auxURL := range cfg.ProxyAuxURLs {
		client, err := rpc.NewClient(auxURL, logger)
		if err != nil {
			return nil, fmt.Errorf("aux rpc: %w", err)
		}
		s.auxRPCs = append(s.auxRPCs, client)
	}
	if cfg.ShareLogPath != "" {
		f, err := os.OpenFile(cfg.ShareLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			return nil, fmt.Errorf("open share log: %w", err)
		}
		s.shareFile = f
	}
	return s, nil
}

func payoutScript(cfg *Config) ([]byte, error) {
	if cfg.TrackerScriptHex != "" {
		return block.ScriptForAddress("hex:" + cfg.TrackerScriptHex)
	}
	return block.ScriptForAddress(cfg.TrackerAddress)
}

func (s *Service) Close() error {
	if s.shareFile != nil {
		return s.shareFile.Close()
	}
	return nil
}

func (s *Service) Run(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	if s.cfg.StartProxy {
		if err := s.startProxy(ctx); err != nil {
			return err
		}
	}
	if err := s.startPoolRPC(ctx); err != nil {
		return err
	}
	if s.cfg.DashboardAddr != "" {
		if err := s.startDashboard(ctx); err != nil {
			return err
		}
	}

	if _, err := s.work.Update(); err != nil {
		s.logger.Warn("initial template update failed", "error", err)
	}
	go s.templateLoop(ctx)

	stratumServer := &stratum.Server{
		Addr:       s.cfg.StratumAddr,
		Pool:       s,
		Logger:     s.logger,
		Difficulty: s.baseDiff,
	}
	return stratumServer.ListenAndServe(ctx)
}

func (s *Service) startProxy(ctx context.Context) error {
	host, portText, err := net.SplitHostPort(s.cfg.ProxyAddr)
	if err != nil {
		return fmt.Errorf("proxy addr: %w", err)
	}
	port, err := strconv.Atoi(portText)
	if err != nil {
		return fmt.Errorf("proxy port: %w", err)
	}
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	cfg := &proxyconfig.Config{
		WorkerPort:         port,
		ParentURLs:         []string{s.cfg.ProxyParentURL},
		AuxURLs:            s.cfg.ProxyAuxURLs,
		AuxPayoutAddresses: s.cfg.ProxyAuxPayouts,
		AuxChainNames:      s.cfg.ProxyAuxNames,
		DebugGotwork:       s.cfg.DebugGotwork,
		MerkleSize:         s.cfg.ProxyMerkleSize,
	}
	listener, err := server.NewListener(cfg, s.logger)
	if err != nil {
		return fmt.Errorf("proxy listener: %w", err)
	}
	ln, err := net.Listen("tcp4", net.JoinHostPort(host, portText))
	if err != nil {
		return fmt.Errorf("proxy listen: %w", err)
	}
	listener.StartBackground(ctx)
	httpServer := &http.Server{
		Handler:           listener,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      3 * time.Minute,
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    16 << 10,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
	}()
	go func() {
		s.logger.Info("embedded merged-mining proxy listening", "addr", ln.Addr().String())
		if err := httpServer.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			s.logger.Error("proxy server failed", "error", err)
		}
	}()
	return nil
}

func (s *Service) startPoolRPC(ctx context.Context) error {
	ln, err := net.Listen("tcp4", s.cfg.JSONRPCAddr)
	if err != nil {
		return fmt.Errorf("pool rpc listen: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleRPC)
	httpServer := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    16 << 10,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
	}()
	go func() {
		s.logger.Info("pool JSON-RPC listening", "addr", ln.Addr().String())
		if err := httpServer.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			s.logger.Error("pool JSON-RPC failed", "error", err)
		}
	}()
	return nil
}

func (s *Service) templateLoop(ctx context.Context) {
	ticker := time.NewTicker(s.cfg.PollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := s.work.Update(); err != nil {
				s.logger.Warn("template update failed", "error", err)
			}
		}
	}
}

func (s *Service) CurrentJob(username string) *work.Job {
	auxHex := ""
	if s.cfg.RequireProxyReady {
		// Do not hand out public stratum work until the merged-mining proxy has
		// a usable aux template. This prevents miners from hashing work that
		// cannot be submitted to aux chains.
		aux, err := s.getUsableAuxFor(username)
		if err != nil {
			s.logger.Warn("proxy aux not ready; no stratum job sent", "miner", username, "error", err)
			return nil
		}
		auxHex = aux
	}
	job, err := s.work.JobWithAux(auxHex)
	if err != nil {
		s.logger.Warn("no usable work template", "miner", username, "error", err)
		return nil
	}
	s.storeJob(username, job)
	return job
}

func (s *Service) RegisterMiner(username, remote string) {
	s.minersMu.Lock()
	defer s.minersMu.Unlock()
	state := s.miners[username]
	state.Username = username
	state.Remote = remote
	state.LastSeen = time.Now()
	s.miners[username] = state
}

func (s *Service) SubmitShare(ctx context.Context, sub share.Submission) share.Result {
	job := s.lookupJob(sub.Username, sub.JobID)
	if job == nil {
		s.rejected.Add(1)
		return share.Rejected("unknown-work")
	}
	if s.cfg.RequireProxyReady {
		if !s.hasRecentAux(recentAuxGrace) {
			s.rejected.Add(1)
			return share.Rejected("gotwork-unavailable")
		}
	}
	// Rebuild the exact coinbase and parent header the miner hashed. All
	// accept/reject decisions below are made from this reconstructed header.
	coinbase, err := hex.DecodeString(job.Coinbase1 + sub.ExtraNonce1 + sub.ExtraNonce2 + job.Coinbase2)
	if err != nil {
		s.rejected.Add(1)
		return share.Rejected("bad-extranonce")
	}
	merkleRoot, err := block.CoinbaseMerkleRoot(coinbase, job.Branches)
	if err != nil {
		s.rejected.Add(1)
		return share.Rejected("bad-merkle")
	}
	header, err := block.BuildHeader(job.Template.Version, job.Template.PreviousBlockLE, merkleRoot, sub.NTime, job.Template.BitsLE, sub.Nonce)
	if err != nil {
		s.rejected.Add(1)
		return share.Rejected("malformed-submit")
	}
	powHash := hash.Blake256R8(header)
	hashValue := block.HashValueLE(powHash[:])
	shareTarget, _ := block.TargetFromHex(job.TargetHex)
	if shareTarget == nil || hashValue.Cmp(shareTarget) > 0 {
		s.rejected.Add(1)
		return share.Rejected("high-hash")
	}

	// A share can be valid for pool accounting without meeting the parent
	// block target. Only parent-target shares are assembled and submitted to
	// Blakecoin with submitblock.
	parentTarget, err := block.CompactTarget(job.Template.BitsHex)
	parentTargetMet := err == nil && hashValue.Cmp(parentTarget) <= 0
	parentAccepted := false
	parentStatus := "parent-rejected"
	if parentTargetMet {
		payload, err := block.AssembleBlock(header, coinbase, job.Template.Transactions)
		if err != nil {
			parentStatus = "parent-assemble-failed"
			s.logger.Warn("failed to assemble parent block", "error", err)
		} else {
			raw, err := s.parent.Call(ctx, "submitblock", hex.EncodeToString(payload))
			parentAccepted, parentStatus = parentSubmitStatus(raw, err)
			if parentAccepted {
				s.parentFound.Add(1)
			} else if err != nil {
				s.logger.Warn("parent submitblock failed", "status", parentStatus, "error", err)
			} else {
				s.logger.Warn("parent submitblock rejected", "status", parentStatus, "result", strings.TrimSpace(string(raw)))
			}
		}
	}
	auxAccepted := false
	if s.shouldForwardGotwork(hashValue, parentTargetMet) {
		// Parent or aux-target shares are forwarded to the proxy as gotwork.
		// The proxy builds and submits chain-specific AuxPoW payloads.
		auxAccepted = s.forwardGotwork(ctx, sub.Username, powHash[:], header, coinbase, job.Branches, parentAccepted, parentStatus)
	} else {
		s.gotworkSkipped.Add(1)
		if s.cfg.DebugGotwork {
			s.logger.Debug("share is below aux target; skipping gotwork submit",
				"username", sub.Username,
				"hash", hex.EncodeToString(powHash[:]),
				"parent_status", parentStatus,
			)
		}
	}
	s.accepted.Add(1)
	s.markMinerShare(sub.Username, parentAccepted || auxAccepted)
	s.logShare(sub, job, powHash[:], parentAccepted)
	return share.Accepted(job.TargetHex, hex.EncodeToString(reverseCopy(powHash[:])))
}

func parentSubmitStatus(raw json.RawMessage, err error) (bool, string) {
	if err != nil {
		return false, "parent-submit-failed"
	}
	text := strings.TrimSpace(string(raw))
	if text == "" || text == "null" {
		return true, "parent-accepted"
	}
	var reject string
	if json.Unmarshal(raw, &reject) == nil {
		if strings.TrimSpace(reject) == "" {
			return true, "parent-accepted"
		}
		return false, "parent-rejected"
	}
	return false, "parent-rejected"
}

func (s *Service) getAuxFor(username string) (string, error) {
	var raw json.RawMessage
	var err error
	if username == "" {
		raw, err = s.proxy.Call(context.Background(), "getaux")
	} else {
		// Per-miner getaux lets the proxy derive SegWit-capable aux payout
		// addresses from v2 mining keys where each daemon supports them.
		raw, err = s.proxy.Call(context.Background(), "getaux", map[string]interface{}{"username": username})
	}
	if err != nil {
		return "", err
	}
	var resp types.GetAuxResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return "", err
	}
	if resp.Error != "" {
		return "", errors.New(resp.Error)
	}
	auxHex := resp.CoinbaseAux
	if auxHex == "" && resp.Aux != "" {
		auxHex = "fabe6d6d" + resp.Aux
	}
	if auxHex == "" {
		return "", errors.New("proxy response missing aux template")
	}
	auxTarget, targetErr := parseProxyAuxTarget(resp.AuxTarget)
	if resp.AuxTarget != "" && targetErr != nil {
		s.logger.Warn("proxy returned invalid aux target", "error", targetErr)
	}
	s.auxMu.Lock()
	s.lastAux = auxHex
	s.lastAuxAt = time.Now()
	if auxTarget != nil {
		s.lastAuxTarget = new(big.Int).Set(auxTarget)
	}
	s.auxMu.Unlock()
	return auxHex, nil
}

func parseProxyAuxTarget(targetHex string) (*big.Int, error) {
	targetHex = strings.TrimSpace(targetHex)
	if targetHex == "" {
		return nil, nil
	}
	// The embedded proxy returns aux_target in Eloipool byte order for
	// compatibility with getaux clients. Convert it back before numeric
	// comparison with HashValueLE, otherwise aux-valid shares are skipped.
	if len(targetHex) == 64 {
		targetHex = merkle.ReverseChunks(targetHex, 2)
	}
	return block.TargetFromHex(targetHex)
}

func (s *Service) getUsableAuxFor(username string) (string, error) {
	aux, err := s.getAuxFor(username)
	if err == nil {
		return aux, nil
	}
	// A short grace window keeps mining alive through brief daemon/proxy lag.
	// If the aux template is actually missing or stale, CurrentJob returns nil
	// and stratum stops sending fresh work until the proxy recovers.
	if cached, ok := s.cachedAux(recentAuxGrace); ok {
		s.logger.Warn("using recent cached aux template while proxy refresh recovers", "miner", username, "error", err)
		return cached, nil
	}
	return "", err
}

func (s *Service) cachedAux(maxAge time.Duration) (string, bool) {
	s.auxMu.RLock()
	defer s.auxMu.RUnlock()
	if s.lastAux == "" || s.lastAuxAt.IsZero() || time.Since(s.lastAuxAt) > maxAge {
		return "", false
	}
	return s.lastAux, true
}

func (s *Service) hasRecentAux(maxAge time.Duration) bool {
	_, ok := s.cachedAux(maxAge)
	return ok
}

func (s *Service) shouldForwardGotwork(hashValue *big.Int, parentValid bool) bool {
	if parentValid {
		return true
	}
	if hashValue == nil {
		return true
	}
	s.auxMu.RLock()
	target := s.lastAuxTarget
	if target != nil {
		target = new(big.Int).Set(target)
	}
	s.auxMu.RUnlock()
	if target == nil {
		return true
	}
	// Forward only shares that meet the current aux target. Lower shares still
	// count for pool accounting but do not need expensive aux submissions.
	return hashValue.Cmp(target) <= 0
}

func (s *Service) forwardGotwork(ctx context.Context, username string, powHash []byte, header []byte, coinbase []byte, branches []string, parentValid bool, parentStatus string) bool {
	coinbaseMrkl := append([]byte(nil), coinbase...)
	coinbaseMrkl = append(coinbaseMrkl, powHash...)
	coinbaseMrkl = append(coinbaseMrkl, byte(len(branches)))
	for _, branchHex := range branches {
		branch, err := hex.DecodeString(branchHex)
		if err != nil {
			return false
		}
		coinbaseMrkl = append(coinbaseMrkl, branch...)
	}
	coinbaseMrkl = append(coinbaseMrkl, 0, 0, 0, 0)
	coinbaseMrklHex := hex.EncodeToString(coinbaseMrkl)
	if s.cfg.DebugGotwork {
		auxMarker := strings.Index(coinbaseMrklHex, "fabe6d6d")
		s.logger.Info("gotwork payload debug",
			"username", username,
			"coinbase_mrkl_len", len(coinbaseMrkl),
			"coinbase_mrkl_hex", coinbaseMrklHex,
			"aux_marker_hex_offset", auxMarker,
			"pow_hash", hex.EncodeToString(powHash),
			"header", hex.EncodeToString(header),
			"branch_count", len(branches),
			"parent_status", parentStatus,
		)
	}
	req := types.GotWorkRequest{
		Hash:         hex.EncodeToString(powHash),
		Header:       hex.EncodeToString(header),
		CoinbaseMrkl: coinbaseMrklHex,
		Username:     username,
		ParentValid:  parentValid,
		ParentStatus: parentStatus,
	}
	raw, err := s.proxy.Call(ctx, "gotwork", req)
	if err != nil {
		s.logger.Warn("gotwork submit failed", "error", err)
		return false
	}
	s.gotworkSent.Add(1)
	var accepted bool
	_ = json.Unmarshal(raw, &accepted)
	return accepted
}

func (s *Service) storeJob(username string, job *work.Job) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	s.jobs[username+"|"+job.ID] = job
	if len(s.jobs) <= 4096 {
		return
	}
	cutoff := time.Now().Add(-2 * time.Hour)
	for key, candidate := range s.jobs {
		if candidate.Created.Before(cutoff) {
			delete(s.jobs, key)
		}
	}
}

func (s *Service) lookupJob(username, id string) *work.Job {
	s.jobsMu.RLock()
	defer s.jobsMu.RUnlock()
	if job := s.jobs[username+"|"+id]; job != nil {
		return job
	}
	return s.jobs["|"+id]
}

func (s *Service) markMinerShare(username string, winning bool) {
	s.minersMu.Lock()
	defer s.minersMu.Unlock()
	state := s.miners[username]
	state.Shares++
	if winning {
		state.Blocks++
	}
	state.LastSeen = time.Now()
	s.miners[username] = state
}

func (s *Service) logShare(sub share.Submission, job *work.Job, powHash []byte, parentValid bool) {
	if s.shareFile == nil {
		return
	}
	row := fmt.Sprintf("%s\t%s\t%s\t%s\t%s\t%s\t%s\tparent=%t\n",
		time.Now().UTC().Format(time.RFC3339),
		sub.RemoteAddr,
		sub.Username,
		job.ID,
		strings.ToUpper(hex.EncodeToString(reverseCopy(powHash))),
		job.TargetHex,
		job.Template.BitsHex,
		parentValid,
	)
	_, _ = s.shareFile.WriteString(row)
}

func (s *Service) handleRPC(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	defer r.Body.Close()
	var req types.JSONRPCRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeRPC(w, nil, nil, &types.JSONRPCError{Code: -32700, Message: "Parse error"})
		return
	}
	var params []json.RawMessage
	if len(req.Params) > 0 {
		_ = json.Unmarshal(req.Params, &params)
	}
	switch req.Method {
	case "setworkaux":
		if len(params) >= 2 {
			var aux string
			_ = json.Unmarshal(params[1], &aux)
			s.auxMu.Lock()
			s.lastAux = aux
			s.lastAuxAt = time.Now()
			s.auxMu.Unlock()
		}
		writeRPC(w, req.ID, true, nil)
	case "status":
		writeRPC(w, req.ID, s.Status(), nil)
	default:
		writeRPC(w, req.ID, nil, &types.JSONRPCError{Code: -32601, Message: "Method not found"})
	}
}

func (s *Service) Status() map[string]interface{} {
	s.minersMu.RLock()
	minerCount := len(s.miners)
	s.minersMu.RUnlock()
	current := s.work.Current()
	height := int64(0)
	if current != nil && current.Template != nil {
		height = current.Template.Height
	}
	s.auxMu.RLock()
	auxAge := int64(0)
	if !s.lastAuxAt.IsZero() {
		auxAge = int64(time.Since(s.lastAuxAt).Seconds())
	}
	s.auxMu.RUnlock()
	return map[string]interface{}{
		"height":          height,
		"miners":          minerCount,
		"accepted":        s.accepted.Load(),
		"rejected":        s.rejected.Load(),
		"parent_found":    s.parentFound.Load(),
		"gotwork_sent":    s.gotworkSent.Load(),
		"gotwork_skipped": s.gotworkSkipped.Load(),
		"aux_age":         auxAge,
	}
}

func writeRPC(w http.ResponseWriter, id interface{}, result interface{}, rpcErr *types.JSONRPCError) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(types.JSONRPCResponse{JSONRPC: "2.0", ID: id, Result: result, Error: rpcErr})
}

func reverseCopy(in []byte) []byte {
	out := make([]byte, len(in))
	for i := range in {
		out[i] = in[len(in)-1-i]
	}
	return out
}

var _ stratum.Pool = (*Service)(nil)
