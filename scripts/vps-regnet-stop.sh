#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/blakestream-25.2-mining-test}"
POOL_DIR="${POOL_DIR:-/home/sid/Blakestream-Eliopool-25.2-GO}"
BIN_ROOT="${BIN_ROOT:-/root/blakestream-25.2-independent/bin}"

COINS=(
  "BLC|blakecoin|blakecoind|blakecoin-cli|blakecoin.conf"
  "BBTC|blakebitcoin|blakebitcoind|blakebitcoin-cli|blakebitcoin.conf"
  "ELT|electron|electrond|electron-cli|electron.conf"
  "LIT|lithium|lithiumd|lithium-cli|lithium.conf"
  "PHO|photon|photond|photon-cli|photon.conf"
  "UMO|universalmolecule|universalmoleculed|universalmolecule-cli|universalmolecule.conf"
)

if [[ -f "$ROOT/run/eloipool.pid" ]]; then
  pid="$(cat "$ROOT/run/eloipool.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
fi
pkill -f "$POOL_DIR/bin/eloipool" >/dev/null 2>&1 || true

for spec in "${COINS[@]}"; do
  IFS='|' read -r code bindir daemon cli conf <<<"$spec"
  datadir="$ROOT/datadirs/$bindir"
  conf_file="$datadir/$conf"
  cli_path="$BIN_ROOT/$bindir/$cli"
  if [[ -x "$cli_path" && -f "$conf_file" ]]; then
    "$cli_path" -datadir="$datadir" -conf="$conf_file" stop >/dev/null 2>&1 || true
  fi
done

sleep 3

for spec in "${COINS[@]}"; do
  IFS='|' read -r _code bindir daemon _cli _conf <<<"$spec"
  pkill -f "$BIN_ROOT/$bindir/$daemon -datadir=$ROOT/datadirs/$bindir" >/dev/null 2>&1 || true
done

