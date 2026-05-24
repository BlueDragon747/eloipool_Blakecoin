package pool

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/blakecoin/merged-mine-proxy/internal/chain"
)

type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(value string) error {
	*s = append(*s, value)
	return nil
}

type Config struct {
	StratumAddr       string
	JSONRPCAddr       string
	DashboardAddr     string
	ParentRPCURL      string
	ProxyAddr         string
	TrackerAddress    string
	TrackerScriptHex  string
	ShareLogPath      string
	PoolLogPath       string
	PollInterval      time.Duration
	WorkUpdate        time.Duration
	BaseDifficulty    int
	RequireProxyReady bool
	DebugGotwork      bool
	StartProxy        bool
	ProxyParentURL    string
	ProxyAuxURLs      []string
	ProxyAuxPayouts   []string
	ProxyAuxNames     []string
	ProxyMerkleSize   int
}

func Parse(args []string) (*Config, error) {
	var auxURLs stringSlice
	var auxPayouts stringSlice
	var auxNames stringSlice

	cfg := &Config{}
	fs := flag.NewFlagSet("eliopool", flag.ContinueOnError)
	fs.StringVar(&cfg.StratumAddr, "stratum", fmt.Sprintf("0.0.0.0:%d", chain.DefaultStratumPort), "stratum listen address")
	fs.StringVar(&cfg.JSONRPCAddr, "rpc", fmt.Sprintf("127.0.0.1:%d", chain.DefaultPoolJSONRPCPort), "local pool JSON-RPC listen address")
	fs.StringVar(&cfg.DashboardAddr, "dashboard", "", "dashboard web UI listen address; empty disables the web UI")
	fs.StringVar(&cfg.ParentRPCURL, "parent-rpc", envOr("ELIOPOOL_PARENT_RPC_URL", chain.DefaultRPCURL(chain.ParentRPCUser, chain.ParentRPCPass, chain.Blakecoin.RPCPort)), "Blakecoin parent daemon RPC URL")
	fs.StringVar(&cfg.ProxyAddr, "proxy", fmt.Sprintf("127.0.0.1:%d", chain.DefaultProxyPort), "merged-mining proxy listen address")
	fs.StringVar(&cfg.TrackerAddress, "tracker-address", "", "pool keep/fallback Blakecoin payout address")
	fs.StringVar(&cfg.TrackerScriptHex, "tracker-script", "", "explicit pool payout scriptPubKey hex; overrides --tracker-address")
	fs.StringVar(&cfg.ShareLogPath, "share-log", "share-logfile", "share log path")
	fs.StringVar(&cfg.PoolLogPath, "pool-log", "", "pool runtime log path for dashboard display")
	fs.DurationVar(&cfg.PollInterval, "poll", 5*time.Second, "parent template poll interval")
	fs.DurationVar(&cfg.WorkUpdate, "work-update", 55*time.Second, "stratum work refresh interval")
	fs.IntVar(&cfg.BaseDifficulty, "base-difficulty", 1, "initial stratum share difficulty")
	fs.BoolVar(&cfg.RequireProxyReady, "require-proxy-ready", true, "reject stratum shares until merged-mining proxy returns usable aux templates")
	fs.BoolVar(&cfg.DebugGotwork, "debug-gotwork", false, "log full gotwork coinbase-merkle payloads for auxpow debugging")
	fs.BoolVar(&cfg.StartProxy, "start-proxy", true, "start the embedded Go merged-mining proxy")
	fs.StringVar(&cfg.ProxyParentURL, "proxy-parent-rpc", envOr("ELIOPOOL_PROXY_PARENT_RPC_URL", fmt.Sprintf("http://auxpow:auxpow@127.0.0.1:%d/", chain.DefaultPoolJSONRPCPort)), "pool JSON-RPC URL used by the merged-mining proxy")
	fs.Var(&auxURLs, "aux-rpc", "aux daemon RPC URL; repeat for BBTC, ELT, LIT, PHO, UMO")
	fs.Var(&auxPayouts, "aux-payout", "aux pool payout address; repeat in the same order as --aux-rpc")
	fs.Var(&auxNames, "aux-name", "aux display name; repeat in the same order as --aux-rpc")
	fs.IntVar(&cfg.ProxyMerkleSize, "proxy-merkle-size", 16, "merged-mining proxy merkle size")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if len(auxURLs) == 0 {
		auxURLs = envList("ELIOPOOL_AUX_RPC_URLS")
	}
	if len(auxPayouts) == 0 {
		auxPayouts = envList("ELIOPOOL_AUX_PAYOUTS")
	}
	if len(auxNames) == 0 {
		auxNames = envList("ELIOPOOL_AUX_NAMES")
	}
	if len(auxURLs) == 0 {
		auxURLs = chain.AuxRPCURLs(chain.ParentRPCUser, chain.ParentRPCPass)
	}
	if len(auxNames) == 0 {
		auxNames = chain.AuxNames()
	}
	cfg.ProxyAuxURLs = append([]string(nil), auxURLs...)
	cfg.ProxyAuxPayouts = append([]string(nil), auxPayouts...)
	cfg.ProxyAuxNames = append([]string(nil), auxNames...)
	if strings.TrimSpace(cfg.PoolLogPath) == "" {
		cfg.PoolLogPath = dashboardPoolLogPath(cfg.ShareLogPath)
	}
	if cfg.StartProxy && len(cfg.ProxyAuxPayouts) != len(cfg.ProxyAuxURLs) {
		return nil, fmt.Errorf("--aux-payout count must match --aux-rpc count when --start-proxy is enabled")
	}
	if len(cfg.ProxyAuxNames) != 0 && len(cfg.ProxyAuxNames) != len(cfg.ProxyAuxURLs) {
		return nil, fmt.Errorf("--aux-name count must match --aux-rpc count")
	}
	if cfg.BaseDifficulty <= 0 {
		return nil, fmt.Errorf("--base-difficulty must be positive")
	}
	return cfg, nil
}

func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envList(key string) stringSlice {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return nil
	}
	parts := strings.Split(value, ",")
	out := make(stringSlice, 0, len(parts))
	for _, part := range parts {
		if item := strings.TrimSpace(part); item != "" {
			out = append(out, item)
		}
	}
	return out
}
