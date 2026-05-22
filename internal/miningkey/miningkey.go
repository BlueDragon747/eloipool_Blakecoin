package miningkey

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"golang.org/x/crypto/ripemd160"
)

const bech32Charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

type GeneratedBundle struct {
	PrivateKey       string                   `json:"private_key"`
	PublicKey        string                   `json:"public_key"`
	MiningKey        string                   `json:"mining_key"`
	StratumUsername  string                   `json:"stratum_username"`
	DerivedAddresses map[string]DerivedPayout `json:"derived_addresses"`
}

type DerivedPayout struct {
	Label   string `json:"label"`
	HRP     string `json:"hrp"`
	Address string `json:"address"`
	Type    string `json:"addr_type"`
}

// ResolveV2Payout derives a native witness payout for a bare 40-hex mining
// key username. Non-mining-key usernames return ok=false so callers can keep
// their configured pool-level aux payout address.
func ResolveV2Payout(username, baseAddress string) (address string, ok bool) {
	head := username
	if idx := strings.IndexByte(head, '.'); idx >= 0 {
		head = head[:idx]
	}
	if !isMiningKey(head) {
		return "", false
	}
	hrp := InferHRP(baseAddress)
	if hrp == "" {
		return "", false
	}
	return AddressFromV2MiningKey(strings.ToLower(head), hrp)
}

func GenerateV2Bundle(hrps map[string]string) (*GeneratedBundle, error) {
	priv, err := secp256k1.GeneratePrivateKey()
	if err != nil {
		return nil, fmt.Errorf("generate private key: %w", err)
	}
	pub := priv.PubKey().SerializeCompressed()
	miningKey := Hash160Hex(pub)
	return &GeneratedBundle{
		PrivateKey:       hex.EncodeToString(priv.Serialize()),
		PublicKey:        hex.EncodeToString(pub),
		MiningKey:        miningKey,
		StratumUsername:  miningKey,
		DerivedAddresses: DeriveV2Addresses(miningKey, hrps),
	}, nil
}

func DeriveV2Addresses(miningKeyHex string, hrps map[string]string) map[string]DerivedPayout {
	out := make(map[string]DerivedPayout, len(hrps))
	for label, hrp := range hrps {
		addr, ok := AddressFromV2MiningKey(miningKeyHex, hrp)
		if !ok {
			continue
		}
		out[label] = DerivedPayout{
			Label:   label,
			HRP:     strings.ToLower(strings.TrimSpace(hrp)),
			Address: addr,
			Type:    "bech32",
		}
	}
	return out
}

func MiningKeyV2FromCompressedPubKey(pubKeyHex string) (string, error) {
	pubKeyHex = strings.TrimSpace(pubKeyHex)
	pub, err := hex.DecodeString(pubKeyHex)
	if err != nil {
		return "", fmt.Errorf("invalid pubkey hex")
	}
	if len(pub) != 33 || (pub[0] != 0x02 && pub[0] != 0x03) {
		return "", fmt.Errorf("expected 33-byte compressed secp256k1 pubkey")
	}
	if _, err := secp256k1.ParsePubKey(pub); err != nil {
		return "", fmt.Errorf("invalid secp256k1 pubkey")
	}
	return Hash160Hex(pub), nil
}

func Hash160Hex(data []byte) string {
	first := sha256.Sum256(data)
	h := ripemd160.New()
	_, _ = h.Write(first[:])
	return hex.EncodeToString(h.Sum(nil))
}

// InferHRP extracts the bech32 HRP from an existing chain address.
func InferHRP(address string) string {
	address = strings.TrimSpace(address)
	if address == "" || strings.Contains(address, ":") {
		return ""
	}
	lower := strings.ToLower(address)
	if idx := strings.LastIndexByte(lower, '1'); idx > 0 {
		return lower[:idx]
	}
	return ""
}

func isMiningKey(s string) bool {
	if len(s) != 40 {
		return false
	}
	for _, ch := range s {
		if !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')) {
			return false
		}
	}
	return true
}

// AddressFromV2MiningKey encodes HASH160 bytes as a BIP173 witness-v0 bech32
// address under the supplied chain HRP.
func AddressFromV2MiningKey(miningKeyHex, hrp string) (string, bool) {
	program, err := hex.DecodeString(miningKeyHex)
	if err != nil || len(program) != 20 || hrp == "" {
		return "", false
	}
	data, ok := convertBits(program, 8, 5, true)
	if !ok {
		return "", false
	}
	payload := append([]byte{0}, data...)
	return bech32Encode(strings.ToLower(hrp), payload), true
}

func bech32Encode(hrp string, data []byte) string {
	combined := append(data, bech32CreateChecksum(hrp, data)...)
	var b strings.Builder
	b.Grow(len(hrp) + 1 + len(combined))
	b.WriteString(hrp)
	b.WriteByte('1')
	for _, v := range combined {
		b.WriteByte(bech32Charset[v])
	}
	return b.String()
}

func bech32CreateChecksum(hrp string, data []byte) []byte {
	values := append(bech32HRPExpand(hrp), data...)
	values = append(values, 0, 0, 0, 0, 0, 0)
	polymod := bech32Polymod(values) ^ 1
	checksum := make([]byte, 6)
	for i := 0; i < 6; i++ {
		checksum[i] = byte((polymod >> uint(5*(5-i))) & 31)
	}
	return checksum
}

func bech32HRPExpand(hrp string) []byte {
	out := make([]byte, 0, len(hrp)*2+1)
	for _, ch := range []byte(hrp) {
		out = append(out, ch>>5)
	}
	out = append(out, 0)
	for _, ch := range []byte(hrp) {
		out = append(out, ch&31)
	}
	return out
}

func bech32Polymod(values []byte) uint32 {
	chk := uint32(1)
	generator := [5]uint32{0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3}
	for _, v := range values {
		top := chk >> 25
		chk = (chk&0x1ffffff)<<5 ^ uint32(v)
		for i := 0; i < 5; i++ {
			if ((top >> uint(i)) & 1) != 0 {
				chk ^= generator[i]
			}
		}
	}
	return chk
}

func convertBits(data []byte, fromBits, toBits uint, pad bool) ([]byte, bool) {
	acc := uint(0)
	bits := uint(0)
	maxv := uint((1 << toBits) - 1)
	maxAcc := uint((1 << (fromBits + toBits - 1)) - 1)
	ret := make([]byte, 0, len(data)*int(fromBits)/int(toBits))
	for _, value := range data {
		v := uint(value)
		if v>>fromBits != 0 {
			return nil, false
		}
		acc = ((acc << fromBits) | v) & maxAcc
		bits += fromBits
		for bits >= toBits {
			bits -= toBits
			ret = append(ret, byte((acc>>bits)&maxv))
		}
	}
	if pad {
		if bits > 0 {
			ret = append(ret, byte((acc<<(toBits-bits))&maxv))
		}
	} else if bits >= fromBits || ((acc<<(toBits-bits))&maxv) != 0 {
		return nil, false
	}
	return ret, true
}
