package pool

import (
	"encoding/json"
	"errors"
	"math/big"
	"testing"
	"time"

	"github.com/SidGrip/eliopool-15.21-go/internal/block"
)

func TestShouldForwardGotworkUsesAuxTargetGate(t *testing.T) {
	s := &Service{}
	s.lastAuxTarget = big.NewInt(100)

	if !s.shouldForwardGotwork(big.NewInt(101), true) {
		t.Fatal("parent-valid share must always forward to gotwork")
	}
	if !s.shouldForwardGotwork(big.NewInt(100), false) {
		t.Fatal("share at aux target should forward to gotwork")
	}
	if s.shouldForwardGotwork(big.NewInt(101), false) {
		t.Fatal("share above aux target should not forward to gotwork")
	}
}

func TestShouldForwardGotworkAllowsUnknownAuxTarget(t *testing.T) {
	s := &Service{}
	if !s.shouldForwardGotwork(big.NewInt(101), false) {
		t.Fatal("unknown aux target should preserve old forwarding behavior")
	}
}

func TestParseProxyAuxTargetConvertsEloipoolByteOrder(t *testing.T) {
	proxyTarget := "0000000000000000000000000000000000000000000000009fee060000000000"
	canonicalTarget := "000000000006ee9f000000000000000000000000000000000000000000000000"

	got, err := parseProxyAuxTarget(proxyTarget)
	if err != nil {
		t.Fatal(err)
	}
	want, err := block.TargetFromHex(canonicalTarget)
	if err != nil {
		t.Fatal(err)
	}
	if got.Cmp(want) != 0 {
		t.Fatalf("aux target = %064x, want %064x", got, want)
	}

	direct, err := block.TargetFromHex(proxyTarget)
	if err != nil {
		t.Fatal(err)
	}
	if got.Cmp(direct) == 0 {
		t.Fatal("proxy target was parsed without converting Eloipool byte order")
	}

	s := &Service{lastAuxTarget: got}
	if !s.shouldForwardGotwork(new(big.Int).Sub(want, big.NewInt(1)), false) {
		t.Fatal("share below converted aux target should forward to gotwork")
	}
	if s.shouldForwardGotwork(new(big.Int).Add(want, big.NewInt(1)), false) {
		t.Fatal("share above converted aux target should not forward to gotwork")
	}
}

func TestDashboardMiningKeyHRPsSkipsInactiveBBTCBech32(t *testing.T) {
	hrps := dashboardDefaultMiningKeyHRPs()
	if _, ok := hrps["BlakeBitcoin"]; ok {
		t.Fatal("BBTC bech32 should stay out of generated mining-key payouts until SegWit activates")
	}
	for _, label := range []string{"Blakecoin", "Electron", "Lithium", "Photon", "UniversalMolecule"} {
		if hrps[label] == "" {
			t.Fatalf("expected mining-key HRP for %s", label)
		}
	}
}

func TestDashboardMiningKeyHRPsIncludesActiveBBTCBech32(t *testing.T) {
	chains := []dashboardChain{
		{Label: "BlakeBitcoin", Ticker: "BBTC", HRP: "bbtc", SegwitActive: true},
	}
	hrps := dashboardMiningKeyHRPsFromChains(chains)
	if hrps["BlakeBitcoin"] != "bbtc" {
		t.Fatalf("expected active BBTC HRP, got %#v", hrps["BlakeBitcoin"])
	}
}

func TestDashboardSegwitStatusParsesBIP9AndSoftforkFormats(t *testing.T) {
	status, active := dashboardSegwitStatus(map[string]interface{}{
		"bip9_softforks": map[string]interface{}{
			"segwit": map[string]interface{}{"status": "started"},
		},
	})
	if status != "started" || active {
		t.Fatalf("15.21 started status = %q active=%v", status, active)
	}
	status, active = dashboardSegwitStatus(map[string]interface{}{
		"softforks": map[string]interface{}{
			"segwit": map[string]interface{}{"active": true},
		},
	})
	if status != "active" || !active {
		t.Fatalf("25.x active status = %q active=%v", status, active)
	}
}

func TestParseBaseDifficulty(t *testing.T) {
	cfg, err := Parse([]string{"--start-proxy=false", "--base-difficulty", "32"})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.BaseDifficulty != 32 {
		t.Fatalf("base difficulty = %d", cfg.BaseDifficulty)
	}
	if _, err := Parse([]string{"--start-proxy=false", "--base-difficulty", "0"}); err == nil {
		t.Fatal("expected invalid zero base difficulty")
	}
}

func TestParentSubmitStatusReflectsDaemonResult(t *testing.T) {
	accepted, status := parentSubmitStatus(json.RawMessage("null"), nil)
	if !accepted || status != "parent-accepted" {
		t.Fatalf("null submit result = %v %s", accepted, status)
	}
	accepted, status = parentSubmitStatus(json.RawMessage(`"bad-txn"`), nil)
	if accepted || status != "parent-rejected" {
		t.Fatalf("rejection result = %v %s", accepted, status)
	}
	accepted, status = parentSubmitStatus(nil, errors.New("connection refused"))
	if accepted || status != "parent-submit-failed" {
		t.Fatalf("error result = %v %s", accepted, status)
	}
}

func TestCachedAuxGrace(t *testing.T) {
	s := &Service{}
	if _, ok := s.cachedAux(recentAuxGrace); ok {
		t.Fatal("empty aux cache should not be usable")
	}
	s.lastAux = "fabe6d6d00"
	s.lastAuxAt = time.Now()
	if aux, ok := s.cachedAux(recentAuxGrace); !ok || aux != "fabe6d6d00" {
		t.Fatalf("fresh cached aux = %q %v", aux, ok)
	}
	s.lastAuxAt = time.Now().Add(-recentAuxGrace - time.Second)
	if _, ok := s.cachedAux(recentAuxGrace); ok {
		t.Fatal("stale aux cache should not be usable")
	}
}

func TestParseSolveStatusLogLine(t *testing.T) {
	line := `time=2026-05-22T14:34:26.945Z level=INFO msg=2026-05-22T14:34:26.945263,solve_status,parent-accepted,1,1,1,1,1,766a3730403adc074091b6e8edf7c163ebbe9ef950b358e6ee87000000000000`
	event, ok := parseSolveStatusLogLine(line)
	if !ok {
		t.Fatal("expected solve_status line to parse")
	}
	if !event.ParentValid || event.ParentStatus != "parent-accepted" {
		t.Fatalf("parent status = %q valid=%v", event.ParentStatus, event.ParentValid)
	}
	if event.AcceptedCount != 6 || event.RejectedCount != 0 {
		t.Fatalf("accepted/rejected = %d/%d", event.AcceptedCount, event.RejectedCount)
	}
	if len(event.Chains) != 6 || event.Chains[1].Ticker != "BBTC" || !event.Chains[5].Accepted {
		t.Fatalf("chains = %#v", event.Chains)
	}
	if event.Time != "2026-05-22T14:34:26Z" {
		t.Fatalf("time = %s", event.Time)
	}
}

func TestMergeDashboardSolveEventsPrefersLiveEvents(t *testing.T) {
	fallback := []dashboardSolveEvent{{
		Unix:       10,
		ParentHash: "abc",
	}}
	live := []interface{}{
		map[string]interface{}{
			"unix":        float64(10),
			"parent_hash": "abc",
			"username":    "99d7e50d8652fbcd0da10dadee65d37e9d358b3b.rig1",
		},
	}
	events := mergeDashboardSolveEvents(live, fallback)
	if len(events) != 1 {
		t.Fatalf("events = %d", len(events))
	}
	event, ok := events[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected live map event, got %T", events[0])
	}
	if event["username"] == "" {
		t.Fatalf("live event username was not preserved: %#v", event)
	}
}

func TestDashboardMinerPayoutTotalsAggregateByMiningKey(t *testing.T) {
	key := "99d7e50d8652fbcd0da10dadee65d37e9d358b3b"
	events := []interface{}{
		map[string]interface{}{
			"username": key + ".rig1",
			"chains": []interface{}{
				map[string]interface{}{"label": "Blakecoin", "ticker": "BLC", "accepted": true},
				map[string]interface{}{"label": "Electron", "ticker": "ELT", "accepted": true},
				map[string]interface{}{"label": "Photon", "ticker": "PHO", "accepted": false},
			},
		},
		map[string]interface{}{
			"username": key + ".rig2",
			"chains": []interface{}{
				map[string]interface{}{"label": "Blakecoin", "ticker": "BLC", "accepted": true},
			},
		},
	}
	chains := []dashboardChain{
		{Label: "Blakecoin", Blocks: 1980000},
		{Label: "Electron", Blocks: 6200000},
	}
	payouts := dashboardMinerPayoutTotals(events, chains)[key]
	if len(payouts) == 0 {
		t.Fatal("expected payout rows")
	}
	byTicker := map[string]dashboardMinerPayout{}
	for _, payout := range payouts {
		byTicker[payout.Ticker] = payout
	}
	if byTicker["BLC"].Blocks != 2 || byTicker["BLC"].Amount != "100.00000000" {
		t.Fatalf("BLC payout = %#v", byTicker["BLC"])
	}
	if byTicker["ELT"].Blocks != 1 || byTicker["ELT"].Amount != "5.00000000" {
		t.Fatalf("ELT payout = %#v", byTicker["ELT"])
	}
}
