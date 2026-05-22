package block

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"strings"
)

const DefaultShareTargetHex = "00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

func ShareTargetForDifficulty(diff int) (string, error) {
	if diff <= 0 {
		return "", fmt.Errorf("difficulty must be positive")
	}
	target, err := TargetFromHex(DefaultShareTargetHex)
	if err != nil {
		return "", err
	}
	target.Div(target, big.NewInt(int64(diff)))
	if target.Sign() <= 0 {
		target.SetInt64(1)
	}
	return fmt.Sprintf("%064x", target), nil
}

type CoinbaseParts struct {
	Coinbase1 string
	Coinbase2 string
	ScriptLen int
}

func BuildCoinbaseParts(height int64, value int64, scriptPubKey []byte, tag string, auxHex string, extranonceSize int) (CoinbaseParts, error) {
	if extranonceSize <= 0 {
		extranonceSize = 8
	}
	aux, err := hex.DecodeString(strings.TrimSpace(auxHex))
	if auxHex != "" && err != nil {
		return CoinbaseParts{}, fmt.Errorf("decode coinbase aux: %w", err)
	}
	scriptPrefix := append(encodeScriptNumber(height), []byte(tag)...)
	scriptPrefix = append(scriptPrefix, aux...)
	scriptLen := len(scriptPrefix) + extranonceSize

	var tx []byte
	tx = binary.LittleEndian.AppendUint32(tx, 1)
	tx = append(tx, 1)
	tx = append(tx, make([]byte, 32)...)
	tx = binary.LittleEndian.AppendUint32(tx, 0xffffffff)
	tx = appendVarInt(tx, uint64(scriptLen))
	tx = append(tx, scriptPrefix...)
	coinbase1 := hex.EncodeToString(tx)

	var tail []byte
	tail = binary.LittleEndian.AppendUint32(tail, 0xffffffff)
	tail = append(tail, 1)
	tail = binary.LittleEndian.AppendUint64(tail, uint64(value))
	tail = appendVarInt(tail, uint64(len(scriptPubKey)))
	tail = append(tail, scriptPubKey...)
	tail = binary.LittleEndian.AppendUint32(tail, 0)
	return CoinbaseParts{Coinbase1: coinbase1, Coinbase2: hex.EncodeToString(tail), ScriptLen: scriptLen}, nil
}

func BuildHeader(version uint32, prevLE []byte, merkleRoot []byte, ntimeHex string, bitsLE []byte, nonceHex string) ([]byte, error) {
	if len(prevLE) != 32 {
		return nil, fmt.Errorf("previous block must be 32 bytes, got %d", len(prevLE))
	}
	if len(merkleRoot) != 32 {
		return nil, fmt.Errorf("merkle root must be 32 bytes, got %d", len(merkleRoot))
	}
	ntime, err := hex.DecodeString(ntimeHex)
	if err != nil || len(ntime) != 4 {
		return nil, fmt.Errorf("invalid ntime %q", ntimeHex)
	}
	nonce, err := hex.DecodeString(nonceHex)
	if err != nil || len(nonce) != 4 {
		return nil, fmt.Errorf("invalid nonce %q", nonceHex)
	}
	if len(bitsLE) != 4 {
		return nil, fmt.Errorf("bits must be 4 bytes, got %d", len(bitsLE))
	}
	hdr := make([]byte, 0, 80)
	hdr = binary.LittleEndian.AppendUint32(hdr, version)
	hdr = append(hdr, prevLE...)
	hdr = append(hdr, merkleRoot...)
	hdr = append(hdr, reverseCopy(ntime)...)
	hdr = append(hdr, bitsLE...)
	hdr = append(hdr, reverseCopy(nonce)...)
	return hdr, nil
}

func AssembleBlock(header []byte, coinbase []byte, txHex []string) ([]byte, error) {
	if len(header) != 80 {
		return nil, fmt.Errorf("header must be 80 bytes, got %d", len(header))
	}
	out := append([]byte(nil), header...)
	out = appendVarInt(out, uint64(1+len(txHex)))
	out = append(out, coinbase...)
	for _, rawHex := range txHex {
		raw, err := hex.DecodeString(rawHex)
		if err != nil {
			return nil, fmt.Errorf("decode transaction: %w", err)
		}
		out = append(out, raw...)
	}
	return out, nil
}

func CoinbaseMerkleRoot(coinbase []byte, branches []string) ([]byte, error) {
	root := OneSHA(coinbase)
	for _, branchHex := range branches {
		branch, err := hex.DecodeString(branchHex)
		if err != nil || len(branch) != 32 {
			return nil, fmt.Errorf("invalid merkle branch %q", branchHex)
		}
		root = DoubleSHA(append(root, branch...))
	}
	return root, nil
}

func MerkleBranches(txHex []string) ([]string, error) {
	if len(txHex) == 0 {
		return nil, nil
	}
	level := make([][]byte, 1, len(txHex)+1)
	level[0] = nil
	for _, rawHex := range txHex {
		raw, err := hex.DecodeString(rawHex)
		if err != nil {
			return nil, fmt.Errorf("decode transaction: %w", err)
		}
		txid := OneSHA(raw)
		level = append(level, txid)
	}
	var branches []string
	for len(level) > 1 {
		if len(level) > 1 && level[1] != nil {
			branches = append(branches, hex.EncodeToString(level[1]))
		}
		if len(level)%2 == 1 {
			level = append(level, level[len(level)-1])
		}
		next := [][]byte{nil}
		for i := 2; i < len(level); i += 2 {
			next = append(next, DoubleSHA(append(level[i], level[i+1]...)))
		}
		level = next
	}
	return branches, nil
}

func OneSHA(data []byte) []byte {
	sum := sha256.Sum256(data)
	return sum[:]
}

func DoubleSHA(data []byte) []byte {
	first := sha256.Sum256(data)
	second := sha256.Sum256(first[:])
	return second[:]
}

func CompactTarget(bitsHex string) (*big.Int, error) {
	raw, err := hex.DecodeString(bitsHex)
	if err != nil || len(raw) != 4 {
		return nil, fmt.Errorf("invalid compact bits %q", bitsHex)
	}
	exp := uint(raw[0])
	mantissa := uint32(raw[1])<<16 | uint32(raw[2])<<8 | uint32(raw[3])
	target := new(big.Int).SetUint64(uint64(mantissa))
	if exp <= 3 {
		target.Rsh(target, 8*(3-exp))
	} else {
		target.Lsh(target, 8*(exp-3))
	}
	return target, nil
}

func TargetFromHex(targetHex string) (*big.Int, error) {
	targetHex = strings.TrimSpace(targetHex)
	if len(targetHex) != 64 {
		return nil, errors.New("target must be 32 bytes of hex")
	}
	target := new(big.Int)
	if _, ok := target.SetString(targetHex, 16); !ok {
		return nil, errors.New("invalid target hex")
	}
	return target, nil
}

func HashValueLE(hash []byte) *big.Int {
	return new(big.Int).SetBytes(reverseCopy(hash))
}

func appendVarInt(out []byte, n uint64) []byte {
	switch {
	case n < 0xfd:
		return append(out, byte(n))
	case n <= 0xffff:
		out = append(out, 0xfd)
		return binary.LittleEndian.AppendUint16(out, uint16(n))
	case n <= 0xffffffff:
		out = append(out, 0xfe)
		return binary.LittleEndian.AppendUint32(out, uint32(n))
	default:
		out = append(out, 0xff)
		return binary.LittleEndian.AppendUint64(out, n)
	}
}

func encodeScriptNumber(n int64) []byte {
	if n == 0 {
		return []byte{0}
	}
	var encoded []byte
	value := uint64(n)
	for value > 0 {
		encoded = append(encoded, byte(value&0xff))
		value >>= 8
	}
	if encoded[len(encoded)-1]&0x80 != 0 {
		encoded = append(encoded, 0)
	}
	return append([]byte{byte(len(encoded))}, encoded...)
}

func reverseCopy(in []byte) []byte {
	out := make([]byte, len(in))
	for i := range in {
		out[i] = in[len(in)-1-i]
	}
	return out
}
