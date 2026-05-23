package block

import (
	"encoding/hex"
	"testing"
)

func TestScriptForBech32Address(t *testing.T) {
	script, err := ScriptForAddress("blc1qn8t72rvx2tau6rdppkk7uewn06wntzemgxd4pk")
	if err != nil {
		t.Fatal(err)
	}
	want := "001499d7e50d8652fbcd0da10dadee65d37e9d358b3b"
	if hex.EncodeToString(script) != want {
		t.Fatalf("got %x, want %s", script, want)
	}
}

func TestCompactTarget(t *testing.T) {
	target, err := CompactTarget("1b00a102")
	if err != nil {
		t.Fatal(err)
	}
	want := "a102000000000000000000000000000000000000000000000000"
	if target.Text(16) != want {
		t.Fatalf("got %s, want %s", target.Text(16), want)
	}
}

func TestShareTargetForDifficulty(t *testing.T) {
	diff1, err := ShareTargetForDifficulty(1)
	if err != nil {
		t.Fatal(err)
	}
	if diff1 != DefaultShareTargetHex {
		t.Fatalf("diff1 target = %s", diff1)
	}
	diff2, err := ShareTargetForDifficulty(2)
	if err != nil {
		t.Fatal(err)
	}
	if diff2 >= diff1 {
		t.Fatalf("diff2 target should be lower than diff1: %s >= %s", diff2, diff1)
	}
	if _, err := ShareTargetForDifficulty(0); err == nil {
		t.Fatal("expected invalid zero difficulty")
	}
}
