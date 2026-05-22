package block

import (
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

const bech32Charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

// ScriptForAddress returns a scriptPubKey for a native bech32 witness address
// or an explicit script hex value. 15.21 Blakecoin mining normally uses bech32
// now; operators can pass a script hex when they need a legacy fallback.
func ScriptForAddress(value string) ([]byte, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return []byte{0x51}, nil
	}
	if strings.HasPrefix(value, "hex:") {
		script, err := hex.DecodeString(strings.TrimPrefix(value, "hex:"))
		if err != nil {
			return nil, fmt.Errorf("decode script hex: %w", err)
		}
		return script, nil
	}
	version, program, err := decodeSegwitAddress(value)
	if err != nil {
		return nil, err
	}
	if version > 16 {
		return nil, errors.New("witness version out of range")
	}
	if len(program) < 2 || len(program) > 40 {
		return nil, errors.New("witness program length out of range")
	}
	op := byte(0x00)
	if version > 0 {
		op = 0x50 + byte(version)
	}
	return append([]byte{op, byte(len(program))}, program...), nil
}

func decodeSegwitAddress(addr string) (int, []byte, error) {
	if addr != strings.ToLower(addr) && addr != strings.ToUpper(addr) {
		return 0, nil, errors.New("mixed-case bech32 address")
	}
	addr = strings.ToLower(addr)
	pos := strings.LastIndexByte(addr, '1')
	if pos <= 0 || pos+7 > len(addr) {
		return 0, nil, errors.New("invalid bech32 address")
	}
	payload := addr[pos+1:]
	data := make([]byte, 0, len(payload))
	for _, ch := range payload {
		idx := strings.IndexRune(bech32Charset, ch)
		if idx < 0 {
			return 0, nil, errors.New("invalid bech32 character")
		}
		data = append(data, byte(idx))
	}
	if bech32Polymod(append(bech32HRPExpand(addr[:pos]), data...)) != 1 {
		return 0, nil, errors.New("bech32 checksum mismatch")
	}
	data = data[:len(data)-6]
	if len(data) == 0 {
		return 0, nil, errors.New("missing witness version")
	}
	program, ok := convertBits(data[1:], 5, 8, false)
	if !ok {
		return 0, nil, errors.New("invalid witness program bits")
	}
	return int(data[0]), program, nil
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
