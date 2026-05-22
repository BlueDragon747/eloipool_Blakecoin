package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseFlags_DefaultsRejectMissingAuxPayout(t *testing.T) {
	_, err := ParseFlags([]string{})
	if err == nil {
		t.Fatal("expected default aux URL to require a matching aux payout address")
	}
}

func TestParseFlags_CustomValues(t *testing.T) {
	cfg, err := ParseFlags([]string{
		"-w", "8888",
		"-p", "http://user:pass@127.0.0.1:8332/",
		"-p", "http://user:pass@127.0.0.1:8333/",
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
		"-a", "tbbtc1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7afa890n",
		"-a", "telt1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7aza9heu",
		"--per-solver-aux-payouts",
		"-s", "8",
		"-r",
		"-i", "/run/proxy.pid",
		"-l", "/var/log/proxy.log",
	})
	if err != nil {
		t.Fatalf("ParseFlags failed: %v", err)
	}

	if cfg.WorkerPort != 8888 {
		t.Errorf("Expected worker port 8888, got %d", cfg.WorkerPort)
	}

	if len(cfg.ParentURLs) != 2 {
		t.Errorf("Expected 2 parent URLs, got %d", len(cfg.ParentURLs))
	}

	if len(cfg.AuxURLs) != 2 {
		t.Errorf("Expected 2 aux URLs, got %d", len(cfg.AuxURLs))
	}
	if len(cfg.AuxPayoutAddresses) != 2 {
		t.Errorf("Expected 2 aux payout addresses, got %d", len(cfg.AuxPayoutAddresses))
	}
	if !cfg.PerSolverAuxPayouts {
		t.Error("Expected per-solver aux payouts to be enabled")
	}

	if cfg.MerkleSize != 8 {
		t.Errorf("Expected merkle size 8, got %d", cfg.MerkleSize)
	}

	if cfg.RewriteTarget != 32 {
		t.Errorf("Expected rewrite target 32, got %d", cfg.RewriteTarget)
	}

	if cfg.PIDFile != "/run/proxy.pid" {
		t.Errorf("Expected PID file /run/proxy.pid, got %s", cfg.PIDFile)
	}

	if cfg.LogFile != "/var/log/proxy.log" {
		t.Errorf("Expected log file /var/log/proxy.log, got %s", cfg.LogFile)
	}
}

func TestParseFlags_AutoMerkleSize(t *testing.T) {
	cfg, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
		"-x", "http://aux3:pass@127.0.0.1:8344/",
		"-a", "addr1",
		"-a", "addr2",
		"-a", "addr3",
	})
	if err != nil {
		t.Fatalf("ParseFlags failed: %v", err)
	}

	// 3 aux chains -> smallest power of 2 >= 3 is 4
	if cfg.MerkleSize != 4 {
		t.Errorf("Expected auto merkle size 4, got %d", cfg.MerkleSize)
	}
}

func TestParseFlags_ConfigFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxy.json")
	body := []byte(`{
		"worker_port": 19335,
		"parent_urls": ["http://pool:secret@127.0.0.1:19334/"],
		"aux_urls": ["http://node:secret@127.0.0.1:8243/", "http://node:secret@127.0.0.1:6852/"],
		"aux_payout_addresses": ["bbtc_addr", "elt_addr"],
		"aux_chain_names": ["BlakeBitcoin", "Electron"],
		"per_solver_aux_payouts": true,
		"merkle_size": 16,
		"rewrite_target": 32,
		"log_file": "/var/log/proxy.log"
	}`)
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}

	cfg, err := ParseFlags([]string{"--config", path})
	if err != nil {
		t.Fatalf("ParseFlags failed: %v", err)
	}
	if cfg.WorkerPort != 19335 {
		t.Fatalf("worker port = %d", cfg.WorkerPort)
	}
	if len(cfg.ParentURLs) != 1 || cfg.ParentURLs[0] != "http://pool:secret@127.0.0.1:19334/" {
		t.Fatalf("parent urls = %v", cfg.ParentURLs)
	}
	if len(cfg.AuxURLs) != 2 || len(cfg.AuxPayoutAddresses) != 2 {
		t.Fatalf("aux urls/payouts = %v / %v", cfg.AuxURLs, cfg.AuxPayoutAddresses)
	}
	if len(cfg.AuxChainNames) != 2 || cfg.AuxChainNames[1] != "Electron" {
		t.Fatalf("aux chain names = %v", cfg.AuxChainNames)
	}
	if !cfg.PerSolverAuxPayouts || cfg.MerkleSize != 16 || cfg.RewriteTarget != 32 {
		t.Fatalf("unexpected config: %+v", cfg)
	}
}

func TestParseFlags_ConfigFileAllowsFlagOverrides(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxy.json")
	body := []byte(`{
		"worker_port": 19335,
		"parent_urls": ["http://pool:secret@127.0.0.1:19334/"],
		"aux_urls": ["http://node:secret@127.0.0.1:8243/"],
		"aux_payout_addresses": ["file_addr"]
	}`)
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}

	cfg, err := ParseFlags([]string{"--config", path, "-w", "19336", "-x", "http://override:secret@127.0.0.1:9999/", "-a", "override_addr"})
	if err != nil {
		t.Fatalf("ParseFlags failed: %v", err)
	}
	if cfg.WorkerPort != 19336 {
		t.Fatalf("worker port = %d", cfg.WorkerPort)
	}
	if len(cfg.AuxURLs) != 1 || cfg.AuxURLs[0] != "http://override:secret@127.0.0.1:9999/" {
		t.Fatalf("aux urls = %v", cfg.AuxURLs)
	}
}

func TestParseFlags_ConflictingRewriteFlags(t *testing.T) {
	_, err := ParseFlags([]string{"-r", "-R"})
	if err == nil {
		t.Error("Expected error for conflicting rewrite flags")
	}
}

func TestParseFlags_MerkleSizeTooSmall(t *testing.T) {
	_, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
		"-a", "addr1",
		"-a", "addr2",
		"-s", "1",
	})
	if err == nil {
		t.Error("Expected error when merkle size is smaller than number of aux chains")
	}
}

func TestParseFlags_AuxPayoutAddressCountMustMatch(t *testing.T) {
	_, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
		"-a", "addr1",
	})
	if err == nil {
		t.Error("Expected error when aux payout count does not match aux URL count")
	}
}

func TestParseFlags_AuxPayoutAddressRequiredForEveryAuxURL(t *testing.T) {
	_, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
	})
	if err == nil {
		t.Error("Expected error when aux payout addresses are missing")
	}
}

func TestParseFlags_AuxPayoutAddressMustNotBeEmpty(t *testing.T) {
	_, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-a", " ",
	})
	if err == nil {
		t.Error("Expected error when aux payout address is empty")
	}
}

func TestParseFlags_AuxChainNamesMustMatchAuxURLs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxy.json")
	body := []byte(`{
		"aux_urls": ["http://aux1:pass@127.0.0.1:8342/", "http://aux2:pass@127.0.0.1:8343/"],
		"aux_payout_addresses": ["addr1", "addr2"],
		"aux_chain_names": ["Photon"]
	}`)
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}
	_, err := ParseFlags([]string{"--config", path})
	if err == nil {
		t.Error("Expected error when aux chain name count does not match aux URL count")
	}
}

func TestParseFlags_MerkleSizeMustBePowerOfTwo(t *testing.T) {
	_, err := ParseFlags([]string{
		"-x", "http://aux1:pass@127.0.0.1:8342/",
		"-x", "http://aux2:pass@127.0.0.1:8343/",
		"-a", "addr1",
		"-a", "addr2",
		"-s", "3",
	})
	if err == nil {
		t.Error("Expected error when merkle size is not a power of two")
	}
}
