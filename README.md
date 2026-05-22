# Eliopool 15.21 Go

This repository builds the BlakeStream 15.21 Eloipool stack in Go: a Stratum
pool and merged-mining proxy designed to run together. The proxy can also be
built as a standalone binary for focused testing.

## Binaries

```bash
make build
```

Builds:

- `bin/eliopool` - Go Stratum pool with local pool JSON-RPC, optional embedded proxy, and optional dashboard.
- `bin/merged-mine-proxy` - standalone Go merged-mining proxy.

## Ports

- Stratum: `3334`
- Pool JSON-RPC: `19334`
- Merged-mining proxy: `19335`
- Dashboard: `8080` when started with `-dashboard 0.0.0.0:8080`
- Blakecoin parent RPC: `8772`
- Aux RPC defaults:
  - BlakeBitcoin: `8243`
  - Electron: `6852`
  - Lithium: `12000`
  - Photon: `8984`
  - UniversalMolecule: `5921`

## Run Example

The embedded proxy needs one aux payout address per aux daemon.
Generate the BBTC aux payout as legacy until BBTC SegWit activates; the 15.21
daemon rejects native bech32 for that pool address before activation.

```bash
blakebitcoin-cli getnewaddress pool-aux legacy

bin/eliopool \
  -dashboard 0.0.0.0:8080 \
  -tracker-address blc1qn8t72rvx2tau6rdppkk7uewn06wntzemgxd4pk \
  -aux-payout <legacy-bbtc-pool-address> \
  -aux-payout elt1qn8t72rvx2tau6rdppkk7uewn06wntzemgq5f3e \
  -aux-payout lit1qn8t72rvx2tau6rdppkk7uewn06wntzemk42ax2 \
  -aux-payout pho1qn8t72rvx2tau6rdppkk7uewn06wntzemd5jvkk \
  -aux-payout umo1qn8t72rvx2tau6rdppkk7uewn06wntzem78ugrq
```

Use `-tracker-script <scriptPubKeyHex>` if the parent payout cannot be
represented as a bech32 witness address.
For installer-style automation, use the same fallback rule as the Python
15.21 deploy path: try `getnewaddress pool-aux bech32`, and if the daemon
returns a SegWit/bech32 activation error, retry `getnewaddress pool-aux legacy`.
The dashboard mining-key generator also omits BBTC from the bech32-derived
address set until BBTC SegWit activation.

BBTC readiness note: low `verificationprogress` with matching `blocks` and
`headers` is not caused by the legacy payout fallback or by SegWit waiting to
activate. Treat the 15.21 BBTC value as a daemon progress estimate, not as the
pool's only mining-readiness gate. The pool should rely on healthy RPC
templates, usable aux templates, and matching blocks/headers.

## Current Milestone

Implemented:

- Real Blakecoin PoW hash: BLAKE-256 with 8 rounds.
- Parent `getblocktemplate` polling.
- Stratum subscribe, authorize, submit, difficulty, and notify.
- Coinbase split with proxy aux data embedded in the coinbase script.
- Blakecoin share validation and parent `submitblock` on parent-valid blocks.
- Embedded Go merged-mining proxy with `getaux` and `gotwork`.
- Local pool JSON-RPC `setworkaux` and `status`.
- Native Go dashboard with merged-chain status, wallet rows, connected miners,
  recent shares, how-to-mine help, and mining-key generation/derivation APIs.
- Tests for Blake hash fixtures, bech32 payout scripts, compact targets, proxy behavior, and Stratum formatting.

Still needs live parity testing against real 15.21 daemons before replacing
the Python pool:

- Full longpoll/new-template fanout behavior under active miners.
- Production vardiff parity with Python Eloipool.
- SegWit witness commitment parity when block templates contain witness transactions.
- SQL share logging and payout accounting parity, if this Go process is used without the existing dashboard/accounting layer.

## Verify

```bash
gofmt -w .
go test ./...
go build -o bin/eliopool ./cmd/eliopool
go build -o bin/merged-mine-proxy ./cmd/merged-mine-proxy
```
