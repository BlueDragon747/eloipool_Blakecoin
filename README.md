# Eloipool 25.2 Go

This repository builds the BlakeStream 25.2 Eloipool stack in Go: a Stratum
pool and merged-mining proxy designed to run together. The proxy can also be
built as a standalone binary for focused testing.

## Binaries

```bash
make build
```

Builds:

- `bin/eloipool` - Go Stratum pool with local pool JSON-RPC, optional embedded proxy, and optional dashboard.
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

## AuxPoW RPC Contract

The 25.2 Go proxy uses the explicit 25.2 AuxPoW RPC path only:

```text
createauxblock(address) -> submitauxblock(hash, auxpow)
```

Each configured aux daemon requires one matching aux payout address. The Go
proxy does not fall back to legacy no-address `getauxblock` behavior.

## Run Example

The embedded proxy needs one aux payout address per aux daemon.

```bash
bin/eloipool \
  -dashboard 0.0.0.0:8080 \
  -tracker-address blc1q3ylalmce4hmxglwfdvgpe39xkhx4zy6xqpc3a2 \
  -aux-payout bbtc1qadey359wq6gv3lqr4u6y8uexx8rmled697qhr4 \
  -aux-payout elt1qshys68jh6mecz7gp0qnd9xw5kgq4sdncaepz8f \
  -aux-payout lit1qrcc8tv6u504exjwd6eya3xfz8gn0ymmzuaqlan \
  -aux-payout pho1q8y078xag89me3vexdc7q33mzasqyljrkw8hl0s \
  -aux-payout umo1qy7z5lkwmrwg93843malhcunghqluvhu3tdwn5t
```

For service managers, the same sensitive RPC values can be supplied through
environment variables instead of process-visible command-line flags:

- `ELIOPOOL_PARENT_RPC_URL`
- `ELIOPOOL_PROXY_PARENT_RPC_URL`
- `ELIOPOOL_AUX_RPC_URLS` as a comma-separated list
- `ELIOPOOL_AUX_PAYOUTS` as a comma-separated list
- `ELIOPOOL_AUX_NAMES` as a comma-separated list

Use `-tracker-script <scriptPubKeyHex>` if the parent payout cannot be
represented as a bech32 witness address.

## Current Milestone

Implemented:

- Real Blakecoin PoW hash: BLAKE-256 with 8 rounds.
- Parent `getblocktemplate` polling.
- Stratum subscribe, authorize, submit, difficulty, and notify.
- Coinbase split with proxy aux data embedded in the coinbase script.
- Blakecoin share validation and parent `submitblock` on parent-valid blocks.
- Embedded Go merged-mining proxy with `getaux` and `gotwork`.
- Strict `createauxblock` template fetch and `submitauxblock` AuxPoW submission.
- Local pool JSON-RPC `setworkaux` and `status`.
- Native Go dashboard with merged-chain status, wallet rows, connected miners,
  recent shares, how-to-mine help, and mining-key generation/derivation APIs.
- Tests for Blake hash fixtures, bech32 payout scripts, compact targets, proxy behavior, and Stratum formatting.

Live mainnet testing has confirmed miner shares, parent solve detection, and
accepted AuxPoW submissions across the 25.2 aux daemons.

## Verify

```bash
gofmt -w .
go test ./...
go build -o bin/eloipool ./cmd/eloipool
go build -o bin/merged-mine-proxy ./cmd/merged-mine-proxy
```
