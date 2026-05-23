package merkle

import (
	"testing"
)

func TestReverseChunks(t *testing.T) {
	tests := []struct {
		input    string
		chunkLen int
		expected string
	}{
		{"abcdef", 2, "efcdab"},
		{"1234567890", 2, "9078563412"},
		{"aabbccdd", 2, "ddccbbaa"},
	}

	for _, test := range tests {
		result := ReverseChunks(test.input, test.chunkLen)
		if result != test.expected {
			t.Errorf("ReverseChunks(%s, %d) = %s, expected %s",
				test.input, test.chunkLen, result, test.expected)
		}
	}
}

func TestNewTree_SingleLeaf(t *testing.T) {
	leaves := []string{"abc123"}
	tree := NewTree(leaves)

	if len(tree.Detail()) != 1 {
		t.Errorf("Expected 1 detail element, got %d", len(tree.Detail()))
	}

	if tree.Root() != "abc123" {
		t.Errorf("Expected root abc123, got %s", tree.Root())
	}
}

func TestNewTreeCheckedRejectsBadHex(t *testing.T) {
	if _, err := NewTreeChecked([]string{"not-hex"}); err == nil {
		t.Fatal("expected bad hex error")
	}
}

func TestNewTree_MultipleLeaves(t *testing.T) {
	leaves := []string{
		"0000000000000000000000000000000000000000000000000000000000000000",
		"1111111111111111111111111111111111111111111111111111111111111111",
	}

	tree := NewTree(leaves)
	expectedDetail := []string{
		"0000000000000000000000000000000000000000000000000000000000000000",
		"1111111111111111111111111111111111111111111111111111111111111111",
		"d5b353a5d92c85406dfd5cf68e4f0e77da28a6d903cc1eb63bf5ebfe00497e12",
	}
	if len(tree.Detail()) != len(expectedDetail) {
		t.Fatalf("Expected %d detail entries, got %d", len(expectedDetail), len(tree.Detail()))
	}
	for i := range expectedDetail {
		if tree.Detail()[i] != expectedDetail[i] {
			t.Fatalf("detail[%d] = %s, expected %s", i, tree.Detail()[i], expectedDetail[i])
		}
	}
	if tree.Root() != expectedDetail[len(expectedDetail)-1] {
		t.Errorf("Root = %s, expected %s", tree.Root(), expectedDetail[len(expectedDetail)-1])
	}
}

func TestBranch(t *testing.T) {
	leaves := []string{
		"0000000000000000000000000000000000000000000000000000000000000000",
		"1111111111111111111111111111111111111111111111111111111111111111",
		"2222222222222222222222222222222222222222222222222222222222222222",
		"3333333333333333333333333333333333333333333333333333333333333333",
	}

	tree := NewTree(leaves)
	detail := tree.Detail()

	branch := Branch(detail, 0, 4)
	expected := []string{
		"1111111111111111111111111111111111111111111111111111111111111111",
		"a3b916608afe957e34063e2253d49027536edf473f62d82891974f94fca5c569",
	}
	if len(branch) != len(expected) {
		t.Fatalf("Expected branch len %d, got %d", len(expected), len(branch))
	}
	for i := range expected {
		if branch[i] != expected[i] {
			t.Fatalf("branch[%d] = %s, expected %s", i, branch[i], expected[i])
		}
	}
}

func TestBranchCheckedRejectsIncompleteTree(t *testing.T) {
	if branch, ok := BranchChecked([]string{"00"}, 1, 4); ok || branch != nil {
		t.Fatalf("expected incomplete branch rejection, got branch=%v ok=%v", branch, ok)
	}
}

func TestHashPair(t *testing.T) {
	a := []byte{0x00, 0x01, 0x02, 0x03}
	b := []byte{0x04, 0x05, 0x06, 0x07}

	hash := hashPair(a, b)
	if len(hash) != 32 {
		t.Errorf("Expected 32-byte hash, got %d bytes", len(hash))
	}
}

func TestReverseBytes(t *testing.T) {
	input := []byte{0x01, 0x02, 0x03, 0x04}
	expected := []byte{0x04, 0x03, 0x02, 0x01}

	result := reverseBytes(input)
	if len(result) != len(expected) {
		t.Fatalf("Expected length %d, got %d", len(expected), len(result))
	}

	for i := range expected {
		if result[i] != expected[i] {
			t.Errorf("Byte %d: expected %x, got %x", i, expected[i], result[i])
		}
	}
}
