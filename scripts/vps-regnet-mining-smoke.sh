#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/blakestream-25.2-mining-test}"
POOL_DIR="${POOL_DIR:-/home/sid/Blakestream-Eliopool-25.2-GO}"
BIN_ROOT="${BIN_ROOT:-/root/blakestream-25.2-independent/bin}"
RPC_USER="${RPC_USER:-user}"
RPC_PASS="${RPC_PASS:-pass}"
POOL_STRATUM="${POOL_STRATUM:-127.0.0.1:3334}"
POOL_RPC="${POOL_RPC:-127.0.0.1:19334}"
POOL_PROXY="${POOL_PROXY:-127.0.0.1:19335}"
POOL_DASHBOARD="${POOL_DASHBOARD:-127.0.0.1:18080}"
SHARE_COUNT="${SHARE_COUNT:-1}"
SOLVE_COUNT="${SOLVE_COUNT:-1}"
INCLUDE_BBTC="${INCLUDE_BBTC:-1}"
REORG_CHECK="${REORG_CHECK:-0}"
INVALID_AUXPOW_CHECK="${INVALID_AUXPOW_CHECK:-0}"
MINER_USER="${MINER_USER:-783c0d31dbf6d7a5bd3d9f9b8e4b3efef0dbe123.regnet}"

COINS=(
  "BLC|Blakecoin|blakecoin|blakecoind|blakecoin-cli|blakecoin.conf|61320|61420"
  "ELT|Electron|electron|electrond|electron-cli|electron.conf|61324|61424"
  "LIT|Lithium|lithium|lithiumd|lithium-cli|lithium.conf|61326|61426"
  "PHO|Photon|photon|photond|photon-cli|photon.conf|61328|61428"
  "UMO|UniversalMolecule|universalmolecule|universalmoleculed|universalmolecule-cli|universalmolecule.conf|61330|61430"
)
if [[ "$INCLUDE_BBTC" == "1" ]]; then
  COINS=(
    "BLC|Blakecoin|blakecoin|blakecoind|blakecoin-cli|blakecoin.conf|61320|61420"
    "BBTC|BlakeBitcoin|blakebitcoin|blakebitcoind|blakebitcoin-cli|blakebitcoin.conf|61322|61422"
    "ELT|Electron|electron|electrond|electron-cli|electron.conf|61324|61424"
    "LIT|Lithium|lithium|lithiumd|lithium-cli|lithium.conf|61326|61426"
    "PHO|Photon|photon|photond|photon-cli|photon.conf|61328|61428"
    "UMO|UniversalMolecule|universalmolecule|universalmoleculed|universalmolecule-cli|universalmolecule.conf|61330|61430"
  )
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence="$ROOT/evidence/regnet-eliopool-$timestamp"
mkdir -p "$ROOT/datadirs" "$ROOT/run" "$ROOT/logs" "$evidence"

"$POOL_DIR/scripts/vps-regnet-stop.sh" >/dev/null 2>&1 || true
rm -rf "$ROOT/datadirs" "$ROOT/run" "$ROOT/logs"
mkdir -p "$ROOT/datadirs" "$ROOT/run" "$ROOT/logs" "$evidence"

split_spec() {
  IFS='|' read -r CODE NAME BINDIR DAEMON CLI CONF RPC_PORT P2P_PORT <<<"$1"
  DAEMON_PATH="$BIN_ROOT/$BINDIR/$DAEMON"
  CLI_PATH="$BIN_ROOT/$BINDIR/$CLI"
  DATADIR="$ROOT/datadirs/$BINDIR"
  CONF_FILE="$DATADIR/$CONF"
  PID_FILE="$ROOT/run/$BINDIR.pid"
  LOG_FILE="$ROOT/logs/$BINDIR.log"
}

write_conf() {
  mkdir -p "$DATADIR"
  cat > "$CONF_FILE" <<EOF_CONF
regtest=1
server=1
fallbackfee=0.0001
testactivationheight=bip34@1
testactivationheight=dersig@1
testactivationheight=cltv@1
testactivationheight=csv@1
testactivationheight=segwit@1

[regtest]
listen=1
txindex=1
dnsseed=0
upnp=0
natpmp=0
discover=0
acceptnonstdtxn=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcuser=$RPC_USER
rpcpassword=$RPC_PASS
rpcport=$RPC_PORT
port=$P2P_PORT
EOF_CONF
  chmod 0600 "$CONF_FILE"
}

cli() {
  "$CLI_PATH" -datadir="$DATADIR" -conf="$CONF_FILE" "$@"
}

wait_rpc() {
  for _ in $(seq 1 120); do
    if cli getblockchaininfo >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for $CODE RPC" >&2
  return 1
}

create_pool_wallet() {
  if ! cli listwallets 2>/dev/null | grep -q '"pool"'; then
    cli createwallet pool false false "" false true true >/dev/null 2>&1 || cli loadwallet pool >/dev/null
  fi
}

wallet_cli() {
  "$CLI_PATH" -datadir="$DATADIR" -conf="$CONF_FILE" -rpcwallet=pool "$@"
}

for spec in "${COINS[@]}"; do
  split_spec "$spec"
  if [[ ! -x "$DAEMON_PATH" || ! -x "$CLI_PATH" ]]; then
    echo "Missing binary for $CODE under $BIN_ROOT/$BINDIR" >&2
    exit 1
  fi
  write_conf
  "$DAEMON_PATH" -datadir="$DATADIR" -conf="$CONF_FILE" -daemonwait -pid="$PID_FILE" >"$LOG_FILE" 2>&1
  wait_rpc
  create_pool_wallet
done

declare -A ADDR
declare -A HEIGHT_BEFORE
declare -A HEIGHT_AFTER

for spec in "${COINS[@]}"; do
  split_spec "$spec"
  ADDR[$CODE]="$(wallet_cli getnewaddress "" bech32)"
  HEIGHT_BEFORE[$CODE]="$(cli getblockcount)"
done

parent_rpc="http://$RPC_USER:$RPC_PASS@127.0.0.1:61320/"
aux_args=(
  -aux-name Electron -aux-rpc "http://$RPC_USER:$RPC_PASS@127.0.0.1:61324/" -aux-payout "${ADDR[ELT]}"
  -aux-name Lithium -aux-rpc "http://$RPC_USER:$RPC_PASS@127.0.0.1:61326/" -aux-payout "${ADDR[LIT]}"
  -aux-name Photon -aux-rpc "http://$RPC_USER:$RPC_PASS@127.0.0.1:61328/" -aux-payout "${ADDR[PHO]}"
  -aux-name UniversalMolecule -aux-rpc "http://$RPC_USER:$RPC_PASS@127.0.0.1:61330/" -aux-payout "${ADDR[UMO]}"
)
if [[ "$INCLUDE_BBTC" == "1" ]]; then
  aux_args=(
    -aux-name BlakeBitcoin -aux-rpc "http://$RPC_USER:$RPC_PASS@127.0.0.1:61322/" -aux-payout "${ADDR[BBTC]}"
    "${aux_args[@]}"
  )
fi

"$POOL_DIR/bin/eloipool" \
  -stratum "$POOL_STRATUM" \
  -rpc "$POOL_RPC" \
  -proxy "$POOL_PROXY" \
  -dashboard "$POOL_DASHBOARD" \
  -parent-rpc "$parent_rpc" \
  -tracker-address "${ADDR[BLC]}" \
  -share-log "$ROOT/logs/share-logfile" \
  -pool-log "$ROOT/logs/eloipool.log" \
  -poll 1s \
  -work-update 5s \
  -base-difficulty 1 \
  -share-target 7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff \
  -debug-gotwork \
  "${aux_args[@]}" \
  >"$ROOT/logs/eloipool.stdout" 2>&1 &
echo $! > "$ROOT/run/eloipool.pid"

for _ in $(seq 1 60); do
  if grep -q "stratum listening" "$ROOT/logs/eloipool.log" 2>/dev/null; then
    break
  fi
  sleep 1
done

run_miner_solve() {
  local index="$1"
  STRATUM_HOST="${POOL_STRATUM%:*}" \
  STRATUM_PORT="${POOL_STRATUM##*:}" \
  STRATUM_USER="$MINER_USER" \
  STRATUM_SHARE_COUNT="$SHARE_COUNT" \
  STRATUM_TARGET_MODE=network \
  python3 "$POOL_DIR/deploy-bundle/cpu_miner.py" >"$evidence/cpu-miner-${index}.log" 2>&1
  cat "$evidence/cpu-miner-${index}.log" >>"$evidence/cpu-miner.log"
}

for solve_index in $(seq 1 "$SOLVE_COUNT"); do
  run_miner_solve "$solve_index"
  sleep 1
done

sleep 3

declare -A REORG_OLD
declare -A REORG_NEW
declare -A INVALID_AUXPOW

if [[ "$INVALID_AUXPOW_CHECK" == "1" ]]; then
  for spec in "${COINS[@]}"; do
    split_spec "$spec"
    [[ "$CODE" == "BLC" ]] && continue
    aux_template="$(wallet_cli getnewaddress "" bech32 | xargs -I{} "$CLI_PATH" -datadir="$DATADIR" -conf="$CONF_FILE" createauxblock {} 2>&1 || true)"
    aux_hash="$(printf '%s\n' "$aux_template" | python3 -c 'import json,sys; data=sys.stdin.read();
try:
    print(json.loads(data).get("hash",""))
except Exception:
    print("")
')"
    if [[ -n "$aux_hash" ]]; then
      invalid_result="$("$CLI_PATH" -datadir="$DATADIR" -conf="$CONF_FILE" submitauxblock "$aux_hash" 00 2>&1 || true)"
      INVALID_AUXPOW[$CODE]="$invalid_result"
    else
      INVALID_AUXPOW[$CODE]="createauxblock failed: $aux_template"
    fi
  done
fi

if [[ "$REORG_CHECK" == "1" ]]; then
  for spec in "${COINS[@]}"; do
    split_spec "$spec"
    [[ "$CODE" == "BLC" ]] && continue
    REORG_OLD[$CODE]="$(cli getbestblockhash)"
    cli invalidateblock "${REORG_OLD[$CODE]}" >/dev/null
  done
  run_miner_solve "reorg"
  sleep 3
  for spec in "${COINS[@]}"; do
    split_spec "$spec"
    [[ "$CODE" == "BLC" ]] && continue
    REORG_NEW[$CODE]="$(cli getbestblockhash)"
  done
fi

for spec in "${COINS[@]}"; do
  split_spec "$spec"
  HEIGHT_AFTER[$CODE]="$(cli getblockcount)"
  cli getblockchaininfo > "$evidence/${CODE,,}-blockchaininfo.json"
  wallet_cli getwalletinfo > "$evidence/${CODE,,}-walletinfo.json"
done

{
  echo "# VPS Regtest Eloipool Mining Smoke"
  echo
  echo "Timestamp UTC: $timestamp"
  echo
  echo "| Chain | Payout address | Height before | Height after | Delta |"
  echo "| --- | --- | ---: | ---: | ---: |"
  for spec in "${COINS[@]}"; do
    split_spec "$spec"
    before="${HEIGHT_BEFORE[$CODE]}"
    after="${HEIGHT_AFTER[$CODE]}"
    delta=$((after - before))
    echo "| $NAME | ${ADDR[$CODE]} | $before | $after | $delta |"
  done
  echo
  echo "Included BBTC: $INCLUDE_BBTC"
  echo "Solve count: $SOLVE_COUNT"
  echo "Invalid AuxPoW check: $INVALID_AUXPOW_CHECK"
  echo "Reorg check: $REORG_CHECK"
  echo
  echo "Pool log: $ROOT/logs/eloipool.log"
  echo "Miner log: $evidence/cpu-miner.log"
  echo "Share log: $ROOT/logs/share-logfile"
  if [[ "$INVALID_AUXPOW_CHECK" == "1" ]]; then
    echo
    echo "## Invalid AuxPoW Results"
    echo
    for spec in "${COINS[@]}"; do
      split_spec "$spec"
      [[ "$CODE" == "BLC" ]] && continue
      echo "- $NAME: ${INVALID_AUXPOW[$CODE]}"
    done
  fi
  if [[ "$REORG_CHECK" == "1" ]]; then
    echo
    echo "## Reorg Replacement Results"
    echo
    for spec in "${COINS[@]}"; do
      split_spec "$spec"
      [[ "$CODE" == "BLC" ]] && continue
      echo "- $NAME: ${REORG_OLD[$CODE]} -> ${REORG_NEW[$CODE]}"
    done
  fi
} > "$evidence/summary.md"

for spec in "${COINS[@]}"; do
  split_spec "$spec"
  before="${HEIGHT_BEFORE[$CODE]}"
  after="${HEIGHT_AFTER[$CODE]}"
  min_delta="$SOLVE_COUNT"
  if [[ "$REORG_CHECK" == "1" && "$CODE" == "BLC" ]]; then
    min_delta=$((SOLVE_COUNT + 1))
  fi
  if [[ "$REORG_CHECK" == "1" && "$CODE" != "BLC" ]]; then
    min_delta="$SOLVE_COUNT"
  fi
  if (( after - before < min_delta )); then
    echo "$CODE did not advance enough: before=$before after=$after min_delta=$min_delta" >&2
    exit 1
  fi
  if [[ "$REORG_CHECK" == "1" && "$CODE" != "BLC" && "${REORG_OLD[$CODE]}" == "${REORG_NEW[$CODE]}" ]]; then
    echo "$CODE reorg replacement did not change best hash" >&2
    exit 1
  fi
done

echo "$evidence/summary.md"
