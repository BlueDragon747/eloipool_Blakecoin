package chain

// Coin describes one BlakeStream chain as the pool sees it.
type Coin struct {
	Name    string
	Ticker  string
	HRP     string
	RPCPort int
	P2PPort int
	Parent  bool
}

const (
	ParentRPCUser = "user"
	ParentRPCPass = "pass"

	DefaultPoolJSONRPCPort = 19334
	DefaultProxyPort       = 19335
	DefaultStratumPort     = 3334
)

var Blakecoin = Coin{
	Name:    "Blakecoin",
	Ticker:  "BLC",
	HRP:     "blc",
	RPCPort: 8772,
	P2PPort: 8773,
	Parent:  true,
}

var AuxCoins = []Coin{
	{Name: "BlakeBitcoin", Ticker: "BBTC", HRP: "bbtc", RPCPort: 8243, P2PPort: 8356},
	{Name: "Electron", Ticker: "ELT", HRP: "elt", RPCPort: 6852, P2PPort: 6853},
	{Name: "Lithium", Ticker: "LIT", HRP: "lit", RPCPort: 12000, P2PPort: 12007},
	{Name: "Photon", Ticker: "PHO", HRP: "pho", RPCPort: 8984, P2PPort: 35556},
	{Name: "UniversalMolecule", Ticker: "UMO", HRP: "umo", RPCPort: 5921, P2PPort: 24785},
}

func DefaultRPCURL(user, pass string, port int) string {
	return "http://" + user + ":" + pass + "@127.0.0.1:" + itoa(port) + "/"
}

func AuxRPCURLs(user, pass string) []string {
	urls := make([]string, 0, len(AuxCoins))
	for _, coin := range AuxCoins {
		urls = append(urls, DefaultRPCURL(user, pass, coin.RPCPort))
	}
	return urls
}

func AuxNames() []string {
	names := make([]string, 0, len(AuxCoins))
	for _, coin := range AuxCoins {
		names = append(names, coin.Name)
	}
	return names
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
