package work

import (
	"encoding/json"
	"testing"
)

func TestParseTemplateKeepsTransactionIDsForMerkleBranches(t *testing.T) {
	raw, err := json.Marshal(map[string]interface{}{
		"version":           0x20000000,
		"previousblockhash": "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
		"bits":              "207fffff",
		"curtime":           1,
		"height":            1,
		"coinbasevalue":     50_00000000,
		"transactions": []map[string]interface{}{
			{
				"data": "01000000000000000000",
				"txid": "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f",
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	tpl, err := ParseTemplate(raw)
	if err != nil {
		t.Fatal(err)
	}
	branches, err := merkleBranches(tpl)
	if err != nil {
		t.Fatal(err)
	}
	want := "3f3e3d3c3b3a393837363534333231302f2e2d2c2b2a29282726252423222120"
	if len(branches) != 1 || branches[0] != want {
		t.Fatalf("branches = %#v, want [%s]", branches, want)
	}
}
