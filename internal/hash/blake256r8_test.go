package hash

import (
	"encoding/binary"
	"encoding/hex"
	"testing"

	"github.com/blakecoin/merged-mine-proxy/internal/block"
)

func TestBlakecoinDevnetHeaderFixture(t *testing.T) {
	prev, _ := hex.DecodeString("0000000000000000000000000000000000000000000000000000000000000000")
	merkle, _ := hex.DecodeString("9e4654d5bb91c723c3dbbaee57761d06ed10ac17f4d8841746aeec7ff8206ddc")
	hdr := make([]byte, 0, 80)
	hdr = binary.LittleEndian.AppendUint32(hdr, 1)
	hdr = append(hdr, block.ReverseBytes(prev)...)
	hdr = append(hdr, block.ReverseBytes(merkle)...)
	hdr = binary.LittleEndian.AppendUint32(hdr, 1775683200)
	hdr = binary.LittleEndian.AppendUint32(hdr, 0x1f00ffff)
	hdr = binary.LittleEndian.AppendUint32(hdr, 50151)
	got := Blake256R8(hdr)
	gotBE := block.ReverseBytes(got[:])
	want := "0000c95fc36d84f2cf35a5a5c5666216c782724137c48cf6e3141bba2e089d76"
	if hex.EncodeToString(gotBE) != want {
		t.Fatalf("got %s, want %s", hex.EncodeToString(gotBE), want)
	}
}
