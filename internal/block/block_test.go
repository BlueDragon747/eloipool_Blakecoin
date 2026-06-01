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

func TestEncodeScriptNumberMatchesCoreHeightEncoding(t *testing.T) {
	tests := map[int64]string{
		0:   "00",
		1:   "51",
		16:  "60",
		17:  "0111",
		127: "017f",
		128: "028000",
	}
	for height, want := range tests {
		if got := hex.EncodeToString(encodeScriptNumber(height)); got != want {
			t.Fatalf("height %d script number = %s, want %s", height, got, want)
		}
	}
}

func TestBuildCoinbasePartsUsesCoreBIP34HeightEncoding(t *testing.T) {
	payoutScript, err := hex.DecodeString("001499d7e50d8652fbcd0da10dadee65d37e9d358b3b")
	if err != nil {
		t.Fatal(err)
	}
	parts, err := BuildCoinbaseParts(1, 50_00000000, payoutScript, "/GoEloipool/", "", 8)
	if err != nil {
		t.Fatal(err)
	}
	coinbase1, err := hex.DecodeString(parts.Coinbase1)
	if err != nil {
		t.Fatal(err)
	}
	if len(coinbase1) < 43 {
		t.Fatalf("coinbase1 too short: %d bytes", len(coinbase1))
	}
	if got := coinbase1[42]; got != 0x51 {
		t.Fatalf("coinbase height prefix = %02x, want 51", got)
	}
}

func TestBuildCoinbasePartsCanAppendWitnessCommitmentOutput(t *testing.T) {
	payoutScript, err := hex.DecodeString("001499d7e50d8652fbcd0da10dadee65d37e9d358b3b")
	if err != nil {
		t.Fatal(err)
	}
	commitment, err := hex.DecodeString("6a24aa21a9ed155e46da836f1785c38aa995b9c8d1c1eb9db9c4a307efbf38d176f7ce152563")
	if err != nil {
		t.Fatal(err)
	}
	parts, err := BuildCoinbasePartsWithCommitment(17, 50_00000000, payoutScript, commitment, "/GoEloipool/", "", 8)
	if err != nil {
		t.Fatal(err)
	}
	coinbase2, err := hex.DecodeString(parts.Coinbase2)
	if err != nil {
		t.Fatal(err)
	}
	if len(coinbase2) < 4+1+8+1+len(payoutScript)+8+1+len(commitment)+4 {
		t.Fatalf("coinbase2 too short: %d", len(coinbase2))
	}
	if got := coinbase2[4]; got != 2 {
		t.Fatalf("output count = %d, want 2", got)
	}
	if !bytes.Contains(coinbase2, commitment) {
		t.Fatalf("coinbase2 does not contain witness commitment %x", commitment)
	}
}

func TestAddZeroReservedWitnessToCoinbase(t *testing.T) {
	payoutScript, err := hex.DecodeString("001499d7e50d8652fbcd0da10dadee65d37e9d358b3b")
	if err != nil {
		t.Fatal(err)
	}
	parts, err := BuildCoinbaseParts(17, 50_00000000, payoutScript, "/GoEloipool/", "", 8)
	if err != nil {
		t.Fatal(err)
	}
	legacy, err := RebuildCoinbase(parts.Coinbase1, "00000000", "00000000", parts.Coinbase2)
	if err != nil {
		t.Fatal(err)
	}
	withWitness, err := AddZeroReservedWitnessToCoinbase(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if len(withWitness) != len(legacy)+36 {
		t.Fatalf("witness coinbase length = %d, want %d", len(withWitness), len(legacy)+36)
	}
	if withWitness[4] != 0x00 || withWitness[5] != 0x01 {
		t.Fatalf("missing segwit marker/flag at offset 4: %x", withWitness[4:6])
	}
	witness := withWitness[len(withWitness)-38 : len(withWitness)-4]
	if witness[0] != 1 || witness[1] != 32 {
		t.Fatalf("unexpected coinbase witness prefix: %x", witness[:2])
	}
	if !bytes.Equal(witness[2:], make([]byte, 32)) {
		t.Fatalf("reserved witness value is not zero: %x", witness[2:])
	}
}

func TestMerkleBranchesFromTxIDsUsesInternalByteOrder(t *testing.T) {
	branches, err := MerkleBranchesFromTxIDs([]string{
		"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
	})
	if err != nil {
		t.Fatal(err)
	}
	want := "1f1e1d1c1b1a191817161514131211100f0e0d0c0b0a09080706050403020100"
	if len(branches) != 1 || branches[0] != want {
		t.Fatalf("branches = %#v, want [%s]", branches, want)
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
