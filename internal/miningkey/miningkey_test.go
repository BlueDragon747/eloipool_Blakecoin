package miningkey

import "testing"

func TestAddressFromV2MiningKey(t *testing.T) {
	key := "a5d3e00343efe51e81d39884a74124ca060fefdd"
	got, ok := AddressFromV2MiningKey(key, "tbbtc")
	if !ok {
		t.Fatal("expected address derivation to succeed")
	}
	want := "tbbtc1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7afa890n"
	if got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestResolveV2Payout(t *testing.T) {
	key := "a5d3e00343efe51e81d39884a74124ca060fefdd"
	got, ok := ResolveV2Payout(key+".rig1", "tlit1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7augmrw0")
	if !ok {
		t.Fatal("expected mining-key username to resolve")
	}
	want := "tlit1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7augmrw0"
	if got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestResolveV2PayoutIgnoresNonMiningKey(t *testing.T) {
	if got, ok := ResolveV2Payout("mk2:a5d3e00343efe51e81d39884a74124ca060fefdd", "tbbtc1q5hf7qq6ralj3aqwnnzz2wsfyegrqlm7afa890n"); ok || got != "" {
		t.Fatalf("prefixed mining key should not resolve, got %q ok=%v", got, ok)
	}
}
