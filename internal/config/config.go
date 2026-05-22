package config

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
)

// Config holds all configuration options
type Config struct {
	ConfigFile          string
	WorkerPort          int
	ParentURLs          []string
	AuxURLs             []string
	AuxPayoutAddresses  []string
	AuxChainNames       []string
	PerSolverAuxPayouts bool
	DebugGotwork        bool
	MerkleSize          int
	RewriteTarget       int
	PIDFile             string
	LogFile             string
}

type fileConfig struct {
	WorkerPort          *int     `json:"worker_port"`
	ParentURLs          []string `json:"parent_urls"`
	AuxURLs             []string `json:"aux_urls"`
	AuxPayoutAddresses  []string `json:"aux_payout_addresses"`
	AuxChainNames       []string `json:"aux_chain_names"`
	PerSolverAuxPayouts *bool    `json:"per_solver_aux_payouts"`
	DebugGotwork        *bool    `json:"debug_gotwork"`
	MerkleSize          *int     `json:"merkle_size"`
	RewriteTarget       *int     `json:"rewrite_target"`
	PIDFile             *string  `json:"pid_file"`
	LogFile             *string  `json:"log_file"`
}

// stringSlice is a custom flag.Value that allows multiple -p or -x flags
// to accumulate into a slice, matching the Python argparse behavior.
type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(value string) error {
	*s = append(*s, value)
	return nil
}

// ParseFlags parses command-line flags and returns a Config.
// It mirrors the Python argparse flags exactly.
func ParseFlags(args []string) (*Config, error) {
	var (
		parentURLs         stringSlice
		auxURLs            stringSlice
		auxPayoutAddresses stringSlice
	)

	fs := flag.NewFlagSet("merged-mine-proxy", flag.ContinueOnError)
	fs.SetOutput(nil) // suppress default error output

	cfg := &Config{}

	fs.StringVar(&cfg.ConfigFile, "c", "", "JSON config file path")
	fs.StringVar(&cfg.ConfigFile, "config", "", "JSON config file path")
	fs.IntVar(&cfg.WorkerPort, "w", 9992, "Listen on PORT for RPC connections")
	fs.IntVar(&cfg.WorkerPort, "worker-port", 9992, "Listen on PORT for RPC connections")
	fs.Var(&parentURLs, "p", "Parent chain RPC URL (can be specified multiple times)")
	fs.Var(&parentURLs, "parent-url", "Parent chain RPC URL (can be specified multiple times)")
	fs.Var(&auxURLs, "x", "Aux chain RPC URL (can be specified multiple times)")
	fs.Var(&auxURLs, "aux-url", "Aux chain RPC URL (can be specified multiple times)")
	fs.Var(&auxPayoutAddresses, "a", "Aux payout address matched by order with --aux-url")
	fs.Var(&auxPayoutAddresses, "aux-payout-address", "Aux payout address matched by order with --aux-url")
	fs.BoolVar(&cfg.PerSolverAuxPayouts, "per-solver-aux-payouts", false, "Derive aux payout addresses per miner identity")
	fs.BoolVar(&cfg.DebugGotwork, "debug-gotwork", false, "Log full gotwork coinbase-merkle payloads for auxpow debugging")
	fs.IntVar(&cfg.MerkleSize, "s", 0, "Merkle tree entries (must be power of 2, 0=auto)")
	fs.IntVar(&cfg.MerkleSize, "merkle-size", 0, "Merkle tree entries (must be power of 2, 0=auto)")

	var rewriteTarget32 bool
	var rewriteTarget100 bool
	fs.BoolVar(&rewriteTarget32, "r", false, "Rewrite target difficulty to 32")
	fs.BoolVar(&rewriteTarget32, "rewrite-target", false, "Rewrite target difficulty to 32")
	fs.BoolVar(&rewriteTarget100, "R", false, "Rewrite target difficulty to 100")
	fs.BoolVar(&rewriteTarget100, "rewrite-target-100", false, "Rewrite target difficulty to 100")

	fs.StringVar(&cfg.PIDFile, "i", "", "PID file path")
	fs.StringVar(&cfg.PIDFile, "pidfile", "", "PID file path")
	fs.StringVar(&cfg.LogFile, "l", "", "Log file path")
	fs.StringVar(&cfg.LogFile, "logfile", "", "Log file path")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	fs.Visit(func(f *flag.Flag) {
		seen[f.Name] = true
	})

	if cfg.ConfigFile != "" {
		fileCfg, err := readFileConfig(cfg.ConfigFile)
		if err != nil {
			return nil, err
		}
		if fileCfg.WorkerPort != nil && !flagSeen(seen, "w", "worker-port") {
			cfg.WorkerPort = *fileCfg.WorkerPort
		}
		if len(fileCfg.ParentURLs) > 0 && !flagSeen(seen, "p", "parent-url") {
			parentURLs = append(parentURLs[:0], fileCfg.ParentURLs...)
		}
		if len(fileCfg.AuxURLs) > 0 && !flagSeen(seen, "x", "aux-url") {
			auxURLs = append(auxURLs[:0], fileCfg.AuxURLs...)
		}
		if len(fileCfg.AuxPayoutAddresses) > 0 && !flagSeen(seen, "a", "aux-payout-address") {
			auxPayoutAddresses = append(auxPayoutAddresses[:0], fileCfg.AuxPayoutAddresses...)
		}
		if len(fileCfg.AuxChainNames) > 0 {
			cfg.AuxChainNames = append(cfg.AuxChainNames[:0], fileCfg.AuxChainNames...)
		}
		if fileCfg.PerSolverAuxPayouts != nil && !flagSeen(seen, "per-solver-aux-payouts") {
			cfg.PerSolverAuxPayouts = *fileCfg.PerSolverAuxPayouts
		}
		if fileCfg.DebugGotwork != nil && !flagSeen(seen, "debug-gotwork") {
			cfg.DebugGotwork = *fileCfg.DebugGotwork
		}
		if fileCfg.MerkleSize != nil && !flagSeen(seen, "s", "merkle-size") {
			cfg.MerkleSize = *fileCfg.MerkleSize
		}
		if fileCfg.RewriteTarget != nil && !flagSeen(seen, "r", "rewrite-target", "R", "rewrite-target-100") {
			cfg.RewriteTarget = *fileCfg.RewriteTarget
		}
		if fileCfg.PIDFile != nil && !flagSeen(seen, "i", "pidfile") {
			cfg.PIDFile = *fileCfg.PIDFile
		}
		if fileCfg.LogFile != nil && !flagSeen(seen, "l", "logfile") {
			cfg.LogFile = *fileCfg.LogFile
		}
	}

	if len(parentURLs) == 0 {
		parentURLs = append(parentURLs, "http://un:pw@127.0.0.1:8332/")
	}
	cfg.ParentURLs = parentURLs

	if len(auxURLs) == 0 {
		auxURLs = append(auxURLs, "http://un:pw@127.0.0.1:8342/")
	}
	cfg.AuxURLs = auxURLs
	cfg.AuxPayoutAddresses = auxPayoutAddresses

	if len(cfg.AuxPayoutAddresses) != len(cfg.AuxURLs) {
		return nil, fmt.Errorf("the number of aux payout addresses must match the number of aux URLs")
	}
	for i, payout := range cfg.AuxPayoutAddresses {
		if strings.TrimSpace(payout) == "" {
			return nil, fmt.Errorf("aux payout address %d must not be empty", i)
		}
	}
	if len(cfg.AuxChainNames) > 0 && len(cfg.AuxChainNames) != len(cfg.AuxURLs) {
		return nil, fmt.Errorf("the number of aux chain names must match the number of aux URLs")
	}
	for i, name := range cfg.AuxChainNames {
		if strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("aux chain name %d must not be empty", i)
		}
		cfg.AuxChainNames[i] = strings.TrimSpace(name)
	}

	// Auto-detect merkle size if not specified
	if cfg.MerkleSize == 0 {
		for i := 0; i < 8; i++ {
			if (1 << i) >= len(auxURLs) {
				cfg.MerkleSize = 1 << i
				break
			}
		}
		if cfg.MerkleSize == 0 {
			cfg.MerkleSize = 1 // at least 1
		}
	}

	if len(auxURLs) > cfg.MerkleSize {
		return nil, fmt.Errorf("the merkle size must be at least as large as the number of aux chains")
	}

	if cfg.MerkleSize > 255 {
		return nil, fmt.Errorf("merkle size up to 255")
	}

	if cfg.MerkleSize > 0 && cfg.MerkleSize&(cfg.MerkleSize-1) != 0 {
		return nil, fmt.Errorf("merkle size must be a power of 2")
	}

	// Handle rewrite target flags
	if rewriteTarget32 && rewriteTarget100 {
		return nil, fmt.Errorf("cannot specify both -r and -R")
	}
	if rewriteTarget32 {
		cfg.RewriteTarget = 32
	} else if rewriteTarget100 {
		cfg.RewriteTarget = 100
	}

	return cfg, nil
}

func readFileConfig(path string) (*fileConfig, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config file %q: %w", path, err)
	}
	var cfg fileConfig
	if err := json.Unmarshal(body, &cfg); err != nil {
		return nil, fmt.Errorf("parse config file %q: %w", path, err)
	}
	return &cfg, nil
}

func flagSeen(seen map[string]bool, names ...string) bool {
	for _, name := range names {
		if seen[name] {
			return true
		}
	}
	return false
}
