package merkle

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// Tree represents a merkle tree built from leaf hashes
type Tree struct {
	leaves []string
	detail []string // flattened tree representation matching Python
}

// NewTree builds a merkle tree from a list of hex strings (little-endian byte order).
func NewTree(leaves []string) *Tree {
	tree, _ := NewTreeChecked(leaves)
	return tree
}

// NewTreeChecked builds a merkle tree and returns malformed leaf hex errors.
func NewTreeChecked(leaves []string) (*Tree, error) {
	if len(leaves) == 0 {
		return &Tree{leaves: leaves}, nil
	}

	detail := make([]string, 0, len(leaves)*2)
	level := make([][]byte, len(leaves))
	for i, leaf := range leaves {
		b, err := hex.DecodeString(leaf)
		if err != nil {
			return nil, fmt.Errorf("invalid merkle leaf %d: %w", i, err)
		}
		level[i] = reverseBytes(b)
	}

	for {
		for _, node := range level {
			detail = append(detail, hex.EncodeToString(reverseBytes(node)))
		}
		if len(level) == 1 {
			break
		}
		if len(level)%2 == 1 {
			last := make([]byte, len(level[len(level)-1]))
			copy(last, level[len(level)-1])
			level = append(level, last)
		}
		nextLevel := make([][]byte, 0, len(level)/2)
		for i := 0; i < len(level); i += 2 {
			nextLevel = append(nextLevel, hashPair(level[i], level[i+1]))
		}
		level = nextLevel
	}

	return &Tree{leaves: leaves, detail: detail}, nil
}

// Root returns the merkle root hex string
func (t *Tree) Root() string {
	if len(t.detail) == 0 {
		return ""
	}
	return t.detail[len(t.detail)-1]
}

// Detail returns the flattened tree detail for branch calculation
func (t *Tree) Detail() []string {
	return t.detail
}

// Branch calculates the merkle branch for a given index.
func Branch(tree []string, index, merkleSize int) []string {
	branch, _ := BranchChecked(tree, index, merkleSize)
	return branch
}

// BranchChecked calculates a merkle branch and reports incomplete stale tree
// data instead of silently returning a short proof.
func BranchChecked(tree []string, index, merkleSize int) ([]string, bool) {
	if index < 0 || merkleSize <= 0 {
		return nil, false
	}
	var branch []string
	step := merkleSize
	i1 := index
	j := 0
	for step > 1 {
		i := min(i1^1, step-1)
		if i+j >= len(tree) {
			return nil, false
		}
		branch = append(branch, tree[i+j])
		i1 >>= 1
		j += step
		step = (step + 1) / 2
	}
	return branch, true
}

// hashPair concatenates two hashes and double-sha256s them
func hashPair(a, b []byte) []byte {
	h := sha256.New()
	h.Write(a)
	h.Write(b)
	sum := h.Sum(nil)

	h2 := sha256.New()
	h2.Write(sum)
	return h2.Sum(nil)
}

// reverseBytes reverses a byte slice
func reverseBytes(b []byte) []byte {
	r := make([]byte, len(b))
	for i := range b {
		r[i] = b[len(b)-1-i]
	}
	return r
}

// ReverseChunks reverses a string in chunks of length l (like Python's reverse_chunks)
func ReverseChunks(s string, l int) string {
	chunks := make([]string, 0, len(s)/l)
	for i := 0; i < len(s); i += l {
		end := i + l
		if end > len(s) {
			end = len(s)
		}
		chunks = append(chunks, s[i:end])
	}
	// Reverse order of chunks
	for i, j := 0, len(chunks)-1; i < j; i, j = i+1, j-1 {
		chunks[i], chunks[j] = chunks[j], chunks[i]
	}
	var result strings.Builder
	for _, c := range chunks {
		result.WriteString(c)
	}
	return result.String()
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
