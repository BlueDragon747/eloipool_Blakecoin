package block

import (
	"bytes"
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

func TestRebuildCoinbase(t *testing.T) {
	got, err := RebuildCoinbase("01000000", "12345678", "9abcdef0", "ffffffff")
	if err != nil {
		t.Fatal(err)
	}
	want := []byte{0x01, 0x00, 0x00, 0x00, 0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0, 0xff, 0xff, 0xff, 0xff}
	if !bytes.Equal(got, want) {
		t.Fatalf("coinbase = %x, want %x", got, want)
	}
	if _, err := RebuildCoinbase("00", "11", "not-hex", "22"); err == nil {
		t.Fatal("expected invalid extranonce hex")
	}
}

func TestReverseBytes(t *testing.T) {
	input := []byte{0x01, 0x02, 0x03, 0x04}
	want := []byte{0x04, 0x03, 0x02, 0x01}
	got := ReverseBytes(input)
	if !bytes.Equal(got, want) {
		t.Fatalf("reverse = %x, want %x", got, want)
	}
	if bytes.Equal(input, got) {
		t.Fatal("ReverseBytes should return a reversed copy")
	}
}

func TestReverseWordOrder(t *testing.T) {
	prev := "00000000000000000000000000000000000000000000000000000000deadbeef"
	want := "deadbeef00000000000000000000000000000000000000000000000000000000"
	if got := ReverseWordOrder(prev); got != want {
		t.Fatalf("word order = %s, want %s", got, want)
	}
	odd := "abc"
	if got := ReverseWordOrder(odd); got != odd {
		t.Fatalf("odd word order = %s, want %s", got, odd)
	}
}
