package pool

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	chainparams "github.com/SidGrip/eliopool-15.21-go/internal/chain"
	"github.com/SidGrip/eliopool-15.21-go/internal/miningkey"
	"github.com/SidGrip/eliopool-15.21-go/internal/rpc"
)

type dashboardMiner struct {
	Username       string                 `json:"username"`
	RawUsername    string                 `json:"raw_username"`
	Remote         string                 `json:"remote"`
	LastSeen       string                 `json:"last_seen"`
	Age            int64                  `json:"age"`
	Active         bool                   `json:"active"`
	Shares         int64                  `json:"shares"`
	AcceptedShares int64                  `json:"accepted_shares"`
	Blocks         int64                  `json:"blocks"`
	Address        string                 `json:"addr"`
	AddressType    string                 `json:"addr_type"`
	Worker         string                 `json:"worker"`
	MiningKey      string                 `json:"mining_key"`
	Payouts        []dashboardMinerPayout `json:"payouts"`
}

type dashboardShare struct {
	Time        string `json:"time"`
	Remote      string `json:"remote"`
	Username    string `json:"username"`
	RawUsername string `json:"raw_username"`
	Address     string `json:"addr"`
	AddressType string `json:"addr_type"`
	Worker      string `json:"worker"`
	MiningKey   string `json:"mining_key"`
	JobID       string `json:"job_id"`
	Hash        string `json:"hash"`
	Target      string `json:"target"`
	Bits        string `json:"bits"`
	ParentValid bool   `json:"parent_valid"`
}

type dashboardSolveChainOutcome struct {
	Label     string `json:"label"`
	Ticker    string `json:"ticker"`
	Alias     string `json:"alias"`
	Attempted bool   `json:"attempted"`
	Accepted  bool   `json:"accepted"`
	Status    string `json:"status"`
	Hash      string `json:"hash,omitempty"`
}

type dashboardSolveEvent struct {
	Time          string                       `json:"time"`
	Unix          int64                        `json:"unix"`
	Username      string                       `json:"username,omitempty"`
	ParentHash    string                       `json:"parent_hash"`
	ParentStatus  string                       `json:"parent_status"`
	ParentValid   bool                         `json:"parent_valid"`
	AnyAccepted   bool                         `json:"any_accepted"`
	AcceptedCount int                          `json:"accepted_count"`
	RejectedCount int                          `json:"rejected_count"`
	SkippedCount  int                          `json:"skipped_count"`
	Chains        []dashboardSolveChainOutcome `json:"chains"`
}

type dashboardChain struct {
	Label        string          `json:"label"`
	Ticker       string          `json:"ticker"`
	HRP          string          `json:"hrp"`
	Role         string          `json:"role"`
	Status       string          `json:"status"`
	Chain        string          `json:"chain"`
	Blocks       int64           `json:"blocks"`
	Headers      int64           `json:"headers"`
	Difficulty   string          `json:"difficulty"`
	Connections  int64           `json:"connections"`
	Verify       string          `json:"verify"`
	Segwit       string          `json:"segwit_status,omitempty"`
	SegwitActive bool            `json:"segwit_active"`
	BestHash     string          `json:"best_hash"`
	Wallet       dashboardWallet `json:"wallet"`
	Error        string          `json:"error,omitempty"`
}

type dashboardWallet struct {
	Balance     string `json:"balance"`
	Immature    string `json:"immature"`
	Unconfirmed string `json:"unconfirmed"`
}

type dashboardMinerPayout struct {
	Label     string `json:"label"`
	Ticker    string `json:"ticker"`
	Blocks    int    `json:"blocks"`
	Amount    string `json:"amount"`
	Estimated bool   `json:"estimated"`
}

type dashboardChainClient struct {
	coin   chainparams.Coin
	role   string
	client *rpc.Client
}

func (s *Service) startDashboard(ctx context.Context) error {
	ln, err := net.Listen("tcp4", s.cfg.DashboardAddr)
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	staticHandler, err := dashboardStaticHandler()
	if err != nil {
		return err
	}
	mux.HandleFunc("/", s.handleDashboard)
	mux.Handle("/dashboard/static/", http.StripPrefix("/dashboard/static/", staticHandler))
	mux.HandleFunc("/favicon.ico", handleDashboardFavicon)
	mux.HandleFunc("/api/state", s.handleDashboardState)
	mux.HandleFunc("/api/status", s.handleDashboardState)
	mux.HandleFunc("/api/shares", s.handleDashboardShares)
	mux.HandleFunc("/api/generate-mining-key", s.handleGenerateMiningKey)
	mux.HandleFunc("/api/derive-addresses-v2", s.handleDeriveAddressesV2)
	mux.HandleFunc("/api/derive-address-v2", s.handleDeriveAddressesV2)
	mux.HandleFunc("/api/verify-mining-key", s.handleVerifyMiningKey)
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
		s.logger.Info("dashboard web UI listening", "addr", ln.Addr().String())
		if err := httpServer.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			s.logger.Error("dashboard server failed", "error", err)
		}
	}()
	return nil
}

func (s *Service) handleDashboard(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := renderDashboardHTML(w); err != nil {
		s.logger.Error("dashboard render failed", "error", err)
	}
}

func handleDashboardFavicon(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "image/svg+xml")
	_, _ = w.Write([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#15191e"/><text x="8" y="12" text-anchor="middle" font-family="monospace" font-size="11" font-weight="bold" fill="#35c3ff">B</text></svg>`))
}

func (s *Service) handleDashboardState(w http.ResponseWriter, r *http.Request) {
	proxyStatus := map[string]interface{}{}
	if raw, err := s.proxy.Call(r.Context(), "status"); err == nil {
		_ = json.Unmarshal(raw, &proxyStatus)
	} else {
		proxyStatus["_error"] = dashboardError(err)
	}
	poolLogPath := dashboardPoolLogPath(s.cfg.ShareLogPath)
	recentSolves := mergeDashboardSolveEvents(
		proxyStatus["recent_solves"],
		tailSolveEventsFromPoolLog(poolLogPath, 100),
	)
	chains := s.dashboardChains(r.Context())
	minerPayouts := dashboardMinerPayoutTotals(recentSolves, chains)

	writeJSON(w, map[string]interface{}{
		"status": map[string]interface{}{
			"state": s.dashboardPoolState(proxyStatus),
		},
		"pool":            s.Status(),
		"proxy":           proxyStatus,
		"chains":          chains,
		"miners":          s.dashboardMiners(minerPayouts),
		"recent_shares":   tailShares(s.cfg.ShareLogPath, 80),
		"recent_solves":   recentSolves,
		"pool_log":        tailTextLines(poolLogPath, 80),
		"stratum":         s.dashboardStratum(r),
		"chain_tickers":   dashboardTickers(),
		"chain_order":     dashboardChainOrder(),
		"mining_key_hrps": dashboardMiningKeyHRPsFromChains(chains),
		"updated_at":      time.Now().UTC().Format(time.RFC3339),
	})
}

func (s *Service) handleDashboardShares(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]interface{}{
		"shares":     tailShares(s.cfg.ShareLogPath, 80),
		"updated_at": time.Now().UTC().Format(time.RFC3339),
	})
}

func (s *Service) handleGenerateMiningKey(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	bundle, err := miningkey.GenerateV2Bundle(s.dashboardMiningKeyHRPs(r.Context()))
	if err != nil {
		writeJSON(w, map[string]interface{}{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, map[string]interface{}{"ok": true, "bundle": bundle})
}

func (s *Service) handleDeriveAddressesV2(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body := map[string]string{}
	if r.Method == http.MethodPost {
		_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&body)
	}
	miningKey := strings.TrimSpace(firstNonEmpty(body["mining_key"], r.URL.Query().Get("mining_key")))
	hrp := strings.TrimSpace(firstNonEmpty(body["hrp"], r.URL.Query().Get("hrp")))
	if miningKey == "" {
		writeJSON(w, map[string]interface{}{"ok": false, "error": "mining_key required"})
		return
	}
	hrps := s.dashboardMiningKeyHRPs(r.Context())
	if hrp != "" {
		hrps = map[string]string{"Custom": hrp}
	}
	addresses := miningkey.DeriveV2Addresses(miningKey, hrps)
	if len(addresses) == 0 {
		writeJSON(w, map[string]interface{}{"ok": false, "error": "derivation failed"})
		return
	}
	writeJSON(w, map[string]interface{}{"ok": true, "mining_key": miningKey, "derived_addresses": addresses})
}

func (s *Service) handleVerifyMiningKey(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body := map[string]string{}
	_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&body)
	pubKey := strings.TrimSpace(body["pubkey_hex"])
	miningKey, err := miningkey.MiningKeyV2FromCompressedPubKey(pubKey)
	if err != nil {
		writeJSON(w, map[string]interface{}{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, map[string]interface{}{
		"ok":                true,
		"version":           2,
		"mining_key":        miningKey,
		"derived_addresses": miningkey.DeriveV2Addresses(miningKey, s.dashboardMiningKeyHRPs(r.Context())),
	})
}

func writeJSON(w http.ResponseWriter, payload interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(payload)
}

func (s *Service) dashboardPoolState(proxyStatus map[string]interface{}) string {
	miners := len(s.dashboardMiners(nil))
	if ready, total := dashboardReadyCounts(proxyStatus); total > 0 && ready <= 0 {
		return "waiting for aux"
	}
	if miners > 0 {
		return "mining"
	}
	if height, _ := s.Status()["height"].(int64); height > 0 {
		return "idle"
	}
	return "starting"
}

func (s *Service) dashboardStratum(r *http.Request) map[string]interface{} {
	host := r.Host
	if h, _, err := net.SplitHostPort(host); err == nil && h != "" {
		host = h
	}
	if host == "" {
		host = "127.0.0.1"
	}
	_, portText, err := net.SplitHostPort(s.cfg.StratumAddr)
	if err != nil || portText == "" {
		portText = strconv.Itoa(chainparams.DefaultStratumPort)
	}
	return map[string]interface{}{"host": host, "port": portText}
}

func (s *Service) dashboardMiners(payoutsByIdentity map[string][]dashboardMinerPayout) []dashboardMiner {
	s.minersMu.RLock()
	defer s.minersMu.RUnlock()
	now := time.Now()
	miners := make([]dashboardMiner, 0, len(s.miners))
	for _, state := range s.miners {
		age := int64(now.Sub(state.LastSeen).Seconds())
		if state.LastSeen.IsZero() {
			age = 0
		}
		identity := parseDashboardIdentity(state.Username)
		payouts := payoutsByIdentity[payoutIdentityKey(state.Username)]
		blocks := state.Blocks
		if chainBlocks := totalDashboardPayoutBlocks(payouts); chainBlocks > blocks {
			blocks = chainBlocks
		}
		miners = append(miners, dashboardMiner{
			Username:       displayMinerName(state.Username),
			RawUsername:    state.Username,
			Remote:         displayRemote(state.Remote),
			LastSeen:       state.LastSeen.UTC().Format(time.RFC3339),
			Age:            age,
			Active:         age <= 300,
			Shares:         state.Shares,
			AcceptedShares: state.Shares,
			Blocks:         blocks,
			Address:        identity.Address,
			AddressType:    identity.AddressType,
			Worker:         identity.Worker,
			MiningKey:      identity.MiningKey,
			Payouts:        payouts,
		})
	}
	sort.Slice(miners, func(i, j int) bool {
		if miners[i].Shares == miners[j].Shares {
			return miners[i].Username < miners[j].Username
		}
		return miners[i].Shares > miners[j].Shares
	})
	return miners
}

func totalDashboardPayoutBlocks(payouts []dashboardMinerPayout) int64 {
	var total int64
	for _, payout := range payouts {
		if payout.Blocks > 0 {
			total += int64(payout.Blocks)
		}
	}
	return total
}

func (s *Service) dashboardChainClients() []dashboardChainClient {
	out := []dashboardChainClient{{coin: chainparams.Blakecoin, role: "parent", client: s.parent}}
	for i, coin := range chainparams.AuxCoins {
		var client *rpc.Client
		if i < len(s.auxRPCs) {
			client = s.auxRPCs[i]
		}
		out = append(out, dashboardChainClient{coin: coin, role: "aux", client: client})
	}
	return out
}

func (s *Service) dashboardChains(ctx context.Context) []dashboardChain {
	clients := s.dashboardChainClients()
	rows := make([]dashboardChain, len(clients))
	var wg sync.WaitGroup
	for i, spec := range clients {
		i, spec := i, spec
		wg.Add(1)
		go func() {
			defer wg.Done()
			rows[i] = s.dashboardChain(ctx, spec)
		}()
	}
	wg.Wait()
	return rows
}

func (s *Service) dashboardChain(ctx context.Context, spec dashboardChainClient) dashboardChain {
	row := dashboardChain{
		Label:  spec.coin.Name,
		Ticker: spec.coin.Ticker,
		HRP:    spec.coin.HRP,
		Role:   spec.role,
		Status: "offline",
	}
	if spec.client == nil {
		row.Error = "RPC client not configured"
		return row
	}

	info, err := rpcMap(ctx, spec.client, "getblockchaininfo")
	if err != nil {
		row.Error = dashboardError(err)
		return row
	}
	row.Status = "synced"
	row.Chain = fmt.Sprint(info["chain"])
	row.Blocks = jsonInt(info["blocks"])
	row.Headers = jsonInt(info["headers"])
	if row.Headers == 0 {
		row.Headers = row.Blocks
	}
	row.Difficulty = jsonFloatText(info["difficulty"], 2)
	row.Verify = percentText(jsonFloat(info["verificationprogress"]))
	row.Segwit, row.SegwitActive = dashboardSegwitStatus(info)
	row.BestHash = fmt.Sprint(info["bestblockhash"])
	if row.Headers > row.Blocks {
		row.Status = "syncing"
	}

	if raw, err := rpcRaw(ctx, spec.client, "getconnectioncount"); err == nil {
		var peers int64
		_ = json.Unmarshal(raw, &peers)
		row.Connections = peers
	}
	if wallet, err := rpcMap(ctx, spec.client, "getwalletinfo"); err == nil {
		row.Wallet = dashboardWallet{
			Balance:     jsonFloatText(wallet["balance"], 8),
			Immature:    jsonFloatText(firstJSON(wallet, "immature_balance", "immature"), 8),
			Unconfirmed: jsonFloatText(wallet["unconfirmed_balance"], 8),
		}
	}
	return row
}

func rpcMap(ctx context.Context, client *rpc.Client, method string, params ...interface{}) (map[string]interface{}, error) {
	raw, err := rpcRaw(ctx, client, method, params...)
	if err != nil {
		return nil, err
	}
	out := map[string]interface{}{}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func rpcRaw(ctx context.Context, client *rpc.Client, method string, params ...interface{}) (json.RawMessage, error) {
	callCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	return client.Call(callCtx, method, params...)
}

func tailShares(path string, max int) []dashboardShare {
	if path == "" || max <= 0 {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	lines := make([]string, 0, max)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		lines = append(lines, scanner.Text())
		if len(lines) > max {
			copy(lines, lines[1:])
			lines = lines[:max]
		}
	}
	shares := make([]dashboardShare, 0, len(lines))
	for _, line := range lines {
		parts := strings.Split(line, "\t")
		if len(parts) < 8 {
			continue
		}
		identity := parseDashboardIdentity(parts[2])
		shares = append(shares, dashboardShare{
			Time:        parts[0],
			Remote:      displayRemote(parts[1]),
			Username:    displayMinerName(parts[2]),
			RawUsername: parts[2],
			Address:     identity.Address,
			AddressType: identity.AddressType,
			Worker:      identity.Worker,
			MiningKey:   identity.MiningKey,
			JobID:       parts[3],
			Hash:        parts[4],
			Target:      parts[5],
			Bits:        parts[6],
			ParentValid: strings.TrimSpace(parts[7]) == "parent=true",
		})
	}
	return shares
}

func mergeDashboardSolveEvents(primary interface{}, fallback []dashboardSolveEvent) []interface{} {
	events := make([]interface{}, 0, len(fallback)+16)
	seen := map[string]struct{}{}
	for _, event := range interfaceSolveEvents(primary) {
		key := solveEventKey(event)
		if key != "" {
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
		}
		events = append(events, event)
	}
	for _, event := range fallback {
		key := solveEventKey(event)
		if key != "" {
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
		}
		events = append(events, event)
	}
	sort.SliceStable(events, func(i, j int) bool {
		return solveEventUnix(events[i]) < solveEventUnix(events[j])
	})
	if len(events) > 100 {
		events = events[len(events)-100:]
	}
	return events
}

func interfaceSolveEvents(value interface{}) []interface{} {
	switch events := value.(type) {
	case []interface{}:
		return events
	case []dashboardSolveEvent:
		out := make([]interface{}, 0, len(events))
		for _, event := range events {
			out = append(out, event)
		}
		return out
	default:
		return nil
	}
}

func solveEventKey(value interface{}) string {
	switch event := value.(type) {
	case dashboardSolveEvent:
		return event.ParentHash
	case map[string]interface{}:
		if parentHash, _ := event["parent_hash"].(string); parentHash != "" {
			return parentHash
		}
	}
	return ""
}

func solveEventUnix(value interface{}) int64 {
	switch event := value.(type) {
	case dashboardSolveEvent:
		return event.Unix
	case map[string]interface{}:
		return jsonInt(event["unix"])
	default:
		return 0
	}
}

func dashboardMinerPayoutTotals(events []interface{}, chains []dashboardChain) map[string][]dashboardMinerPayout {
	type total struct {
		blocks int
		amount float64
	}
	chainByLabel := make(map[string]dashboardChain, len(chains))
	for _, chain := range chains {
		chainByLabel[chain.Label] = chain
	}
	order := dashboardChainOrder()
	tickers := dashboardTickers()
	totalsByIdentity := make(map[string]map[string]total)
	for _, event := range events {
		username := solveEventUsername(event)
		identity := payoutIdentityKey(username)
		if identity == "" {
			continue
		}
		if _, ok := totalsByIdentity[identity]; !ok {
			totalsByIdentity[identity] = make(map[string]total)
		}
		for _, outcome := range solveEventChains(event) {
			if !outcome.Accepted {
				continue
			}
			label := outcome.Label
			if label == "" {
				label = labelForTicker(outcome.Ticker)
			}
			if label == "" {
				continue
			}
			chain := chainByLabel[label]
			current := totalsByIdentity[identity][label]
			current.blocks++
			current.amount += dashboardRewardEstimate(label, chain)
			totalsByIdentity[identity][label] = current
		}
	}
	out := make(map[string][]dashboardMinerPayout, len(totalsByIdentity))
	for identity, totals := range totalsByIdentity {
		rows := make([]dashboardMinerPayout, 0, len(order))
		for _, label := range order {
			total := totals[label]
			rows = append(rows, dashboardMinerPayout{
				Label:     label,
				Ticker:    tickers[label],
				Blocks:    total.blocks,
				Amount:    formatDashboardCoins(total.amount),
				Estimated: total.blocks > 0 && dashboardRewardIsEstimate(label),
			})
		}
		out[identity] = rows
	}
	return out
}

func solveEventUsername(value interface{}) string {
	switch event := value.(type) {
	case dashboardSolveEvent:
		return event.Username
	case map[string]interface{}:
		username, _ := event["username"].(string)
		return username
	default:
		return ""
	}
}

func solveEventChains(value interface{}) []dashboardSolveChainOutcome {
	switch event := value.(type) {
	case dashboardSolveEvent:
		return event.Chains
	case map[string]interface{}:
		raw, ok := event["chains"].([]interface{})
		if !ok {
			return nil
		}
		chains := make([]dashboardSolveChainOutcome, 0, len(raw))
		for _, item := range raw {
			m, ok := item.(map[string]interface{})
			if !ok {
				continue
			}
			chains = append(chains, dashboardSolveChainOutcome{
				Label:    stringField(m, "label"),
				Ticker:   stringField(m, "ticker"),
				Alias:    stringField(m, "alias"),
				Accepted: boolField(m, "accepted"),
			})
		}
		return chains
	default:
		return nil
	}
}

func payoutIdentityKey(username string) string {
	username = strings.TrimSpace(username)
	if username == "" {
		return ""
	}
	if i := strings.LastIndex(username, "."); i > 0 {
		username = username[:i]
	}
	if isMiningKeyHex(username) {
		return strings.ToLower(username)
	}
	return username
}

func labelForTicker(ticker string) string {
	ticker = strings.ToUpper(strings.TrimSpace(ticker))
	for label, knownTicker := range dashboardTickers() {
		if knownTicker == ticker {
			return label
		}
	}
	return ""
}

func stringField(m map[string]interface{}, key string) string {
	value, _ := m[key].(string)
	return value
}

func boolField(m map[string]interface{}, key string) bool {
	value, _ := m[key].(bool)
	return value
}

func dashboardRewardEstimate(label string, chain dashboardChain) float64 {
	height := chain.Blocks
	switch label {
	case "Blakecoin":
		return 50
	case "BlakeBitcoin":
		halvings := height / 210000
		if halvings >= 64 {
			return 0
		}
		reward := 50.0
		for i := int64(0); i < halvings; i++ {
			reward /= 2
		}
		return reward
	case "Electron":
		if height > 1051200 {
			return 5
		}
		if height > 525600 {
			return 10
		}
		return 20
	case "Lithium":
		if height < 1999 {
			return 0.48
		}
		if height < 175000 {
			return 48
		}
		if height < 350000 {
			return 24
		}
		if height < 525000 {
			return 12
		}
		if height < 650000 {
			return 6
		}
		if height < 800000 {
			return 3
		}
		if height < 975000 {
			return 1.5
		}
		return 1
	case "Photon":
		return 32768
	case "UniversalMolecule":
		if height == 0 {
			return 1
		}
		if height <= 1440 {
			return 0.001
		}
		return 2
	default:
		return 0
	}
}

func dashboardRewardIsEstimate(label string) bool {
	return label == "Photon" || label == "UniversalMolecule"
}

func formatDashboardCoins(amount float64) string {
	return strconv.FormatFloat(amount, 'f', 8, 64)
}

func dashboardPoolLogPath(shareLogPath string) string {
	shareLogPath = strings.TrimSpace(shareLogPath)
	if shareLogPath == "" {
		return ""
	}
	if strings.Contains(shareLogPath, "go-share-log.tsv") {
		return strings.Replace(shareLogPath, "go-share-log.tsv", "go-pool.log", 1)
	}
	if strings.Contains(shareLogPath, "share-log") {
		return strings.Replace(shareLogPath, "share-log", "go-pool.log", 1)
	}
	return ""
}

func tailTextLines(path string, max int) []string {
	if path == "" || max <= 0 {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	lines := make([]string, 0, max)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		lines = append(lines, redactDashboardLog(scanner.Text()))
		if len(lines) > max {
			copy(lines, lines[1:])
			lines = lines[:max]
		}
	}
	return lines
}

func tailSolveEventsFromPoolLog(path string, max int) []dashboardSolveEvent {
	if path == "" || max <= 0 {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	events := make([]dashboardSolveEvent, 0, max)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		event, ok := parseSolveStatusLogLine(scanner.Text())
		if !ok {
			continue
		}
		events = append(events, event)
		if len(events) > max {
			copy(events, events[1:])
			events = events[:max]
		}
	}
	return events
}

func parseSolveStatusLogLine(line string) (dashboardSolveEvent, bool) {
	const marker = "solve_status,"
	idx := strings.Index(line, marker)
	if idx < 0 {
		return dashboardSolveEvent{}, false
	}
	ts := solveStatusTimestamp(line[:idx])
	if ts.IsZero() {
		return dashboardSolveEvent{}, false
	}
	fields := strings.Split(strings.Trim(strings.TrimSpace(line[idx+len(marker):]), `"`), ",")
	if len(fields) < 7 {
		return dashboardSolveEvent{}, false
	}
	parentHash := strings.Trim(fields[6], `" `)
	if parentHash == "" {
		return dashboardSolveEvent{}, false
	}
	parentStatus := strings.TrimSpace(fields[0])
	parentAccepted := parentStatus == "parent-accepted"
	event := dashboardSolveEvent{
		Time:         ts.Format(time.RFC3339),
		Unix:         ts.Unix(),
		ParentHash:   parentHash,
		ParentStatus: parentStatus,
		ParentValid:  parentAccepted,
	}
	event.Chains = append(event.Chains, dashboardSolveChainOutcome{
		Label:     "Blakecoin",
		Ticker:    "BLC",
		Alias:     "BLC",
		Attempted: true,
		Accepted:  parentAccepted,
		Status:    dashboardAcceptedStatus(parentAccepted, true),
		Hash:      parentHash,
	})
	auxLabels := []struct {
		label  string
		ticker string
		alias  string
	}{
		{"BlakeBitcoin", "BBTC", "MM"},
		{"Electron", "ELT", "MM1"},
		{"Lithium", "LIT", "MM3"},
		{"Photon", "PHO", "MM4"},
		{"UniversalMolecule", "UMO", "MM5"},
	}
	for i, aux := range auxLabels {
		accepted := strings.TrimSpace(fields[i+1]) == "1"
		event.Chains = append(event.Chains, dashboardSolveChainOutcome{
			Label:     aux.label,
			Ticker:    aux.ticker,
			Alias:     aux.alias,
			Attempted: true,
			Accepted:  accepted,
			Status:    dashboardAcceptedStatus(accepted, true),
		})
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
	return event, true
}

func solveStatusTimestamp(prefix string) time.Time {
	idx := strings.LastIndex(prefix, "202")
	if idx < 0 {
		return time.Time{}
	}
	raw := strings.Trim(strings.TrimSuffix(prefix[idx:], ","), `" `)
	raw = strings.TrimPrefix(raw, "msg=")
	raw = strings.TrimSuffix(raw, "Z")
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999999",
		"2006-01-02T15:04:05.999999",
		"2006-01-02T15:04:05",
	} {
		if parsed, err := time.Parse(layout, raw); err == nil {
			return parsed.UTC()
		}
	}
	return time.Time{}
}

func dashboardAcceptedStatus(accepted bool, attempted bool) string {
	if accepted {
		return "accepted"
	}
	if attempted {
		return "rejected"
	}
	return "skipped"
}

func redactDashboardLog(line string) string {
	if strings.Contains(line, "://") && strings.Contains(line, "@") {
		parts := strings.Split(line, "://")
		for i := 1; i < len(parts); i++ {
			if at := strings.Index(parts[i], "@"); at >= 0 {
				parts[i] = "<redacted>@" + parts[i][at+1:]
			}
		}
		line = strings.Join(parts, "://")
	}
	return line
}

type dashboardIdentity struct {
	Address     string
	AddressType string
	Worker      string
	MiningKey   string
}

func parseDashboardIdentity(username string) dashboardIdentity {
	username = strings.TrimSpace(username)
	if username == "" {
		return dashboardIdentity{AddressType: "none"}
	}
	head := username
	worker := ""
	if i := strings.LastIndex(username, "."); i > 0 && i < len(username)-1 {
		head = username[:i]
		worker = username[i+1:]
	}
	head = strings.TrimSpace(head)
	if isMiningKeyHex(head) {
		addresses := miningkey.DeriveV2Addresses(strings.ToLower(head), dashboardDefaultMiningKeyHRPs())
		addr := ""
		if payout, ok := addresses[chainparams.Blakecoin.Name]; ok {
			addr = payout.Address
		}
		return dashboardIdentity{
			Address:     addr,
			AddressType: "bech32",
			Worker:      worker,
			MiningKey:   strings.ToLower(head),
		}
	}
	addrType := classifyDashboardAddress(head)
	if addrType != "none" {
		return dashboardIdentity{
			Address:     head,
			AddressType: addrType,
			Worker:      worker,
		}
	}
	return dashboardIdentity{AddressType: "none", Worker: username}
}

func isMiningKeyHex(value string) bool {
	if len(value) != 40 {
		return false
	}
	for _, ch := range value {
		if !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')) {
			return false
		}
	}
	return true
}

func classifyDashboardAddress(value string) string {
	value = strings.TrimSpace(value)
	lower := strings.ToLower(value)
	if lower != "" && value == lower && strings.Contains(value, "1") {
		hrp, payload, ok := strings.Cut(value, "1")
		if ok && hrp != "" && payload != "" && strings.Contains("blc bbtc elt lit pho umo", hrp) {
			return "bech32"
		}
	}
	if len(value) < 26 || len(value) > 62 {
		return "none"
	}
	for _, ch := range value {
		if !strings.ContainsRune("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", ch) {
			return "none"
		}
	}
	if strings.HasPrefix(value, "3") || strings.HasPrefix(value, "q") {
		return "p2sh"
	}
	return "legacy"
}

func displayMinerName(username string) string {
	username = strings.TrimSpace(username)
	if username == "" {
		return "-"
	}
	key := username
	worker := ""
	if i := strings.LastIndex(username, "."); i > 0 && i < len(username)-1 {
		key = username[:i]
		worker = username[i+1:]
	}
	if len(key) > 18 {
		key = key[:10] + "..." + key[len(key)-6:]
	}
	if worker != "" {
		return key + "." + worker
	}
	return key
}

func displayRemote(remote string) string {
	host, _, err := net.SplitHostPort(remote)
	if err != nil || host == "" {
		host = remote
	}
	if host == "" {
		return "-"
	}
	if strings.Count(host, ".") == 3 {
		parts := strings.Split(host, ".")
		return parts[0] + "." + parts[1] + ".x.x"
	}
	if len(host) > 18 {
		return host[:10] + "..."
	}
	return host
}

func dashboardDefaultMiningKeyHRPs() map[string]string {
	hrps := map[string]string{chainparams.Blakecoin.Name: chainparams.Blakecoin.HRP}
	for _, coin := range chainparams.AuxCoins {
		if !coinMiningKeyBech32Ready(coin) {
			continue
		}
		hrps[coin.Name] = coin.HRP
	}
	return hrps
}

func (s *Service) dashboardMiningKeyHRPs(ctx context.Context) map[string]string {
	hrps := dashboardDefaultMiningKeyHRPs()
	for _, spec := range s.dashboardChainClients() {
		if spec.coin.Ticker != "BBTC" || spec.client == nil {
			continue
		}
		info, err := rpcMap(ctx, spec.client, "getblockchaininfo")
		if err != nil {
			return hrps
		}
		_, active := dashboardSegwitStatus(info)
		if active {
			hrps[spec.coin.Name] = spec.coin.HRP
		}
		return hrps
	}
	return hrps
}

func dashboardMiningKeyHRPsFromChains(chains []dashboardChain) map[string]string {
	hrps := dashboardDefaultMiningKeyHRPs()
	for _, chain := range chains {
		if chain.Ticker == "BBTC" && chain.SegwitActive {
			hrps[chain.Label] = chain.HRP
			break
		}
	}
	return hrps
}

func coinMiningKeyBech32Ready(coin chainparams.Coin) bool {
	// BBTC 15.21 rejects native bech32 pool-aux addresses until SegWit
	// activation, so installs must use a legacy pool-aux fallback for BBTC.
	return coin.Ticker != "BBTC"
}

func dashboardTickers() map[string]string {
	tickers := map[string]string{chainparams.Blakecoin.Name: chainparams.Blakecoin.Ticker}
	for _, coin := range chainparams.AuxCoins {
		tickers[coin.Name] = coin.Ticker
	}
	return tickers
}

func dashboardChainOrder() []string {
	order := []string{chainparams.Blakecoin.Name}
	for _, coin := range chainparams.AuxCoins {
		order = append(order, coin.Name)
	}
	return order
}

func dashboardReadyCounts(proxyStatus map[string]interface{}) (int, int) {
	return int(jsonInt(proxyStatus["ready_count"])), int(jsonInt(proxyStatus["total_chains"]))
}

func dashboardError(err error) string {
	if err == nil {
		return ""
	}
	text := err.Error()
	if strings.Contains(text, "connection refused") || strings.Contains(text, "Connection refused") {
		return "RPC unavailable: daemon is not accepting connections"
	}
	if strings.Contains(text, "context deadline exceeded") || strings.Contains(strings.ToLower(text), "timeout") {
		return "RPC timeout: daemon did not answer in time"
	}
	if strings.Contains(text, "Circuit breaker open") {
		return "RPC temporarily paused after repeated failures"
	}
	return text
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

func firstJSON(m map[string]interface{}, keys ...string) interface{} {
	for _, key := range keys {
		if v, ok := m[key]; ok {
			return v
		}
	}
	return nil
}

func dashboardSegwitStatus(info map[string]interface{}) (string, bool) {
	if softforks, ok := jsonMap(info["bip9_softforks"]); ok {
		if segwit, ok := jsonMap(softforks["segwit"]); ok {
			status := strings.ToLower(strings.TrimSpace(fmt.Sprint(segwit["status"])))
			if status != "" && status != "<nil>" {
				return status, status == "active"
			}
		}
	}
	if softforks, ok := jsonMap(info["softforks"]); ok {
		if segwit, ok := jsonMap(softforks["segwit"]); ok {
			if jsonBool(segwit["active"]) {
				return "active", true
			}
			if status := strings.ToLower(strings.TrimSpace(fmt.Sprint(segwit["status"]))); status != "" && status != "<nil>" {
				return status, status == "active"
			}
			if bip9, ok := jsonMap(segwit["bip9"]); ok {
				status := strings.ToLower(strings.TrimSpace(fmt.Sprint(bip9["status"])))
				if status != "" && status != "<nil>" {
					return status, status == "active"
				}
			}
		}
	}
	return "", false
}

func jsonMap(v interface{}) (map[string]interface{}, bool) {
	m, ok := v.(map[string]interface{})
	return m, ok
}

func jsonBool(v interface{}) bool {
	switch b := v.(type) {
	case bool:
		return b
	case string:
		parsed, _ := strconv.ParseBool(b)
		return parsed
	default:
		return false
	}
}

func jsonInt(v interface{}) int64 {
	switch n := v.(type) {
	case int:
		return int64(n)
	case int64:
		return n
	case float64:
		return int64(n)
	case json.Number:
		i, _ := n.Int64()
		return i
	case string:
		i, _ := strconv.ParseInt(n, 10, 64)
		return i
	default:
		return 0
	}
}

func jsonFloat(v interface{}) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case float32:
		return float64(n)
	case int:
		return float64(n)
	case int64:
		return float64(n)
	case json.Number:
		f, _ := n.Float64()
		return f
	case string:
		f, _ := strconv.ParseFloat(n, 64)
		return f
	default:
		return 0
	}
}

func jsonFloatText(v interface{}, places int) string {
	f := jsonFloat(v)
	if f == 0 {
		return "0"
	}
	return strconv.FormatFloat(f, 'f', places, 64)
}

func percentText(v float64) string {
	if v <= 0 {
		return "-"
	}
	return strconv.FormatFloat(v*100, 'f', 2, 64) + "%"
}
