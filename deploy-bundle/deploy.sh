#!/usr/bin/env bash
# =============================================================================
# BlakeStream Eloipool 15.21 mainnet deploy script
#
# Deploys the released post-SegWit mainnet six-chain merged-mining bundle onto
# a remote VPS that already has all six 0.15.21 daemons running locally:
#   - Blakecoin (parent)
#   - BlakeBitcoin
#   - Electron
#   - Lithium
#   - Photon
#   - UniversalMolecule
#
# The released operator contract is:
#   - bare 40-hex mining keys are the only mining-key username form
#   - child-chain rewards land on pool-controlled addresses on all 5 aux chains
#   - miners are paid proportionally by shares through Eloipool + coinbaser
#   - dashboard shows the pooled aux accounting and derived bech32 payout set
#
# Usage:
#   bash deploy.sh <host> [user] [password]
#
# Useful env overrides:
#   INSTALL_ROOT=/opt/blakecoin-pool
#   LOG_ROOT=/var/log/blakecoin-pool
#   DAEMON_INSTALL_MODE=container|source|existing
#   DAEMON_INSTALL_ROOT=/opt/blakestream-daemons
#   DAEMON_IMAGE_NAMESPACE=sidgrip
#   DAEMON_IMAGE_TAG=15.21
#   STRATUM_PORT=3334
#   DASHBOARD_PORT=8080
#   PUBLIC_HOST=pool.example.com
#   MINING_KEY_SEGWIT_HRP=blc
#   DASH_MINING_KEY_V2_COIN_HRPS='{"BlakeBitcoin":"bbtc",...}'
#   TRACKER_ADDR=B...
#   POOL_AUX_ADDRESS_BBTC=bbtc1...
#   POOL_AUX_ADDRESS_ELT=elt1...
#   POOL_AUX_ADDRESS_LIT=lit1...
#   POOL_AUX_ADDRESS_PHO=pho1...
#   POOL_AUX_ADDRESS_UMO=umo1...
#   BLC_RPC_CONF=/root/.blakecoin/blakecoin.conf
#   BBTC_RPC_CONF=/root/.blakebitcoin/blakebitcoin.conf
#   ...
# =============================================================================
set -euo pipefail

HOST="${1:-}"
USER="${2:-root}"
PASS="${3:-}"

if [ -z "$HOST" ]; then
    echo "Usage: bash deploy.sh <host> [user] [password]"
    exit 1
fi

STRATUM_PORT="${STRATUM_PORT:-3334}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
POOL_JSONRPC_PORT="${POOL_JSONRPC_PORT:-19334}"
PROXY_PORT="${PROXY_PORT:-19335}"
PROXY_MERKLE_SIZE="${PROXY_MERKLE_SIZE:-16}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/blakecoin-pool}"
LOG_ROOT="${LOG_ROOT:-/var/log/blakecoin-pool}"
DAEMON_INSTALL_MODE="${DAEMON_INSTALL_MODE:-existing}"
DAEMON_INSTALL_ROOT="${DAEMON_INSTALL_ROOT:-/opt/blakestream-daemons}"
DAEMON_IMAGE_NAMESPACE="${DAEMON_IMAGE_NAMESPACE:-sidgrip}"
DAEMON_IMAGE_TAG="${DAEMON_IMAGE_TAG:-15.21}"
POOL_USER="${POOL_USER:-blakecoin}"
POOL_GROUP="${POOL_GROUP:-blakecoin}"
PUBLIC_HOST="${PUBLIC_HOST:-$HOST}"
TRACKER_ADDR="${TRACKER_ADDR:-}"
POOL_AUX_ADDRESS_BBTC="${POOL_AUX_ADDRESS_BBTC:-}"
POOL_AUX_ADDRESS_ELT="${POOL_AUX_ADDRESS_ELT:-}"
POOL_AUX_ADDRESS_LIT="${POOL_AUX_ADDRESS_LIT:-}"
POOL_AUX_ADDRESS_PHO="${POOL_AUX_ADDRESS_PHO:-}"
POOL_AUX_ADDRESS_UMO="${POOL_AUX_ADDRESS_UMO:-}"
MINING_KEY_SEGWIT_HRP="${MINING_KEY_SEGWIT_HRP:-blc}"
SERVER_NAME="${SERVER_NAME:-BlakeStream Eloipool 15.21}"
DASH_HEADER_TITLE="${DASH_HEADER_TITLE:-Blakestream Eliopool}"
DASH_HEADER_SUBTITLE="${DASH_HEADER_SUBTITLE:-}"
POOL_SECRET_USER="${POOL_SECRET_USER:-auxpow}"
POOL_SECRET_PASS="${POOL_SECRET_PASS:-auxpow}"
COINBASER_WINDOW="${COINBASER_WINDOW:-500}"
COINBASER_POOL_KEEP_BPS="${COINBASER_POOL_KEEP_BPS:-100}"
UFW_ENABLE="${UFW_ENABLE:-1}"

DEFAULT_V2_COIN_HRPS='{"BlakeBitcoin":"bbtc","Electron":"elt","Lithium":"lit","Photon":"pho","UniversalMolecule":"umo"}'
DEFAULT_CHAIN_TICKERS='{"Blakecoin":"BLC","BlakeBitcoin":"BBTC","Electron":"ELT","Lithium":"LIT","Photon":"PHO","UniversalMolecule":"UMO"}'
DASH_MINING_KEY_V2_COIN_HRPS="${DASH_MINING_KEY_V2_COIN_HRPS:-$DEFAULT_V2_COIN_HRPS}"
DASH_CHAIN_TICKERS="${DASH_CHAIN_TICKERS:-$DEFAULT_CHAIN_TICKERS}"

BLC_RPC_CONF="${BLC_RPC_CONF:-}"
BLC_CLI_BIN="${BLC_CLI_BIN:-}"
BBTC_RPC_CONF="${BBTC_RPC_CONF:-}"
BBTC_CLI_BIN="${BBTC_CLI_BIN:-}"
ELT_RPC_CONF="${ELT_RPC_CONF:-}"
ELT_CLI_BIN="${ELT_CLI_BIN:-}"
LIT_RPC_CONF="${LIT_RPC_CONF:-}"
LIT_CLI_BIN="${LIT_CLI_BIN:-}"
PHO_RPC_CONF="${PHO_RPC_CONF:-}"
PHO_CLI_BIN="${PHO_CLI_BIN:-}"
UMO_RPC_CONF="${UMO_RPC_CONF:-}"
UMO_CLI_BIN="${UMO_CLI_BIN:-}"

BUNDLE_DIR="$(cd "$(dirname "$0")" && pwd)"
ELOIPOOL_SRC="${BUNDLE_DIR}/eloipool"

if [ ! -f "${ELOIPOOL_SRC}/eloipool.py" ]; then
    echo "ERROR: ${ELOIPOOL_SRC}/eloipool.py not found"
    echo "Run scripts/build-bundle.sh to populate the bundle first."
    exit 1
fi

SSH_OPTS=(
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=10
)

run_ssh() {
    if [ -n "$PASS" ]; then
        sshpass -p "$PASS" ssh "${SSH_OPTS[@]}" "${USER}@${HOST}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "${USER}@${HOST}" "$@"
    fi
}

run_scp() {
    if [ -n "$PASS" ]; then
        sshpass -p "$PASS" scp "${SSH_OPTS[@]}" "$@"
    else
        scp "${SSH_OPTS[@]}" "$@"
    fi
}

run_rsync() {
    if [ -n "$PASS" ]; then
        sshpass -p "$PASS" rsync -avz --delete -e "ssh ${SSH_OPTS[*]}" "$@"
    else
        rsync -avz --delete -e "ssh ${SSH_OPTS[*]}" "$@"
    fi
}

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

quote_remote() {
    printf "%q" "$1"
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

escape_env_value() {
    printf '%s' "$1" | sed -e 's/[\\`"$]/\\&/g'
}

py_string_literal() {
    python3 - "$1" <<'PY'
import sys
print(repr(sys.argv[1]))
PY
}

extract_discovery_value() {
    local key="$1"
    printf '%s\n' "$REMOTE_DISCOVERY" | sed -n "s/^${key}=//p" | tail -n1
}

remote_cli_command() {
    local cli="$1"
    local conf="$2"
    shift 2
    local cmd
    cmd="$(quote_remote "$cli") -conf=$(quote_remote "$conf")"
    for arg in "$@"; do
        cmd+=" $(quote_remote "$arg")"
    done
    printf '%s' "$cmd"
}

generate_remote_aux_address() {
    local label="$1"
    local cli="$2"
    local conf="$3"
    local out=""
    local rc=0

    if out="$(run_ssh "$(remote_cli_command "$cli" "$conf" getnewaddress pool-aux bech32)" 2>&1)"; then
        printf '%s\n' "$out"
        return 0
    else
        rc=$?
    fi

    if printf '%s\n' "$out" | grep -Eiq 'bech32|address type|segwit'; then
        echo "WARNING: ${label} rejected bech32 pool-aux address; falling back to legacy" >&2
        out="$(run_ssh "$(remote_cli_command "$cli" "$conf" getnewaddress pool-aux legacy)" 2>&1)"
        printf '%s\n' "$out"
        return
    fi

    printf '%s\n' "$out" >&2
    return "$rc"
}

write_env_line() {
    local file="$1"
    local key="$2"
    local value="$3"
    printf '%s="%s"\n' "$key" "$(escape_env_value "$value")" >> "$file"
}

render_unit_template() {
    local src="$1"
    local dst="$2"
    sed \
        -e "s|@@INSTALL_ROOT@@|$(escape_sed_replacement "$INSTALL_ROOT")|g" \
        -e "s|@@LOG_ROOT@@|$(escape_sed_replacement "$LOG_ROOT")|g" \
        -e "s|@@POOL_USER@@|$(escape_sed_replacement "$POOL_USER")|g" \
        -e "s|@@POOL_GROUP@@|$(escape_sed_replacement "$POOL_GROUP")|g" \
        "$src" > "$dst"
}

provision_remote_daemons() {
    case "$DAEMON_INSTALL_MODE" in
        existing)
            say "Daemon install mode: existing"
            say "Skipping daemon provisioning and using the remote host's existing 0.15.21 daemons"
            return 0
            ;;
        container|source)
            say "Daemon install mode: ${DAEMON_INSTALL_MODE}"
            say "Provisioning the six mainnet daemons before pool discovery"
            run_ssh \
                "DAEMON_INSTALL_MODE=$(quote_remote "$DAEMON_INSTALL_MODE") \
                 DAEMON_INSTALL_ROOT=$(quote_remote "$DAEMON_INSTALL_ROOT") \
                 DAEMON_IMAGE_NAMESPACE=$(quote_remote "$DAEMON_IMAGE_NAMESPACE") \
                 DAEMON_IMAGE_TAG=$(quote_remote "$DAEMON_IMAGE_TAG") \
                 bash -s" < "${BUNDLE_DIR}/scripts/provision-daemons-remote.sh"
            ;;
        *)
            echo "ERROR: unsupported DAEMON_INSTALL_MODE=${DAEMON_INSTALL_MODE} (expected existing, container, or source)" >&2
            exit 1
            ;;
    esac
}

say "Pre-flight: confirming SSH reachability to ${USER}@${HOST}"
run_ssh "echo ok && uname -a && whoami"

provision_remote_daemons

say "Discovering BlakeStream daemon configs and CLI tools on the remote host"
REMOTE_DISCOVERY="$(
    run_ssh \
        "BLC_RPC_CONF=$(quote_remote "$BLC_RPC_CONF") BLC_CLI_BIN=$(quote_remote "$BLC_CLI_BIN") \
         BBTC_RPC_CONF=$(quote_remote "$BBTC_RPC_CONF") BBTC_CLI_BIN=$(quote_remote "$BBTC_CLI_BIN") \
         ELT_RPC_CONF=$(quote_remote "$ELT_RPC_CONF") ELT_CLI_BIN=$(quote_remote "$ELT_CLI_BIN") \
         LIT_RPC_CONF=$(quote_remote "$LIT_RPC_CONF") LIT_CLI_BIN=$(quote_remote "$LIT_CLI_BIN") \
         PHO_RPC_CONF=$(quote_remote "$PHO_RPC_CONF") PHO_CLI_BIN=$(quote_remote "$PHO_CLI_BIN") \
         UMO_RPC_CONF=$(quote_remote "$UMO_RPC_CONF") UMO_CLI_BIN=$(quote_remote "$UMO_CLI_BIN") \
         bash -s" <<'REMOTE'
set -euo pipefail

find_first_file() {
    local candidate
    for candidate in "$@"; do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

find_cli() {
    local override="$1"
    local preferred="$2"
    shift 2
    local candidate
    if [ -n "$override" ] && [ -x "$override" ]; then
        printf '%s\n' "$override"
        return 0
    fi
    if command -v "$preferred" >/dev/null 2>&1; then
        command -v "$preferred"
        return 0
    fi
    for candidate in "$@"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

emit_chain() {
    local prefix="$1"
    local conf_override="$2"
    local cli_override="$3"
    local default_rpc_port="$4"
    local default_p2p_port="$5"
    local preferred_cli="$6"
    local conf_candidates="$7"
    local cli_candidates="$8"
    local conf_path cli_path rpc_user rpc_pass rpc_port p2p_port
    local -a conf_list cli_list

    IFS='|' read -r -a conf_list <<< "$conf_candidates"
    IFS='|' read -r -a cli_list <<< "$cli_candidates"

    if ! conf_path=$(find_first_file "$conf_override" "${conf_list[@]}"); then
        echo "ERROR: could not find ${prefix} RPC conf" >&2
        exit 1
    fi
    if ! cli_path=$(find_cli "$cli_override" "$preferred_cli" "${cli_list[@]}"); then
        echo "ERROR: could not find ${prefix} CLI binary" >&2
        exit 1
    fi

    rpc_user="$(grep -E '^rpcuser=' "$conf_path" | tail -n1 | cut -d= -f2- || true)"
    rpc_pass="$(grep -E '^rpcpassword=' "$conf_path" | tail -n1 | cut -d= -f2- || true)"
    rpc_port="$(grep -E '^rpcport=' "$conf_path" | tail -n1 | cut -d= -f2- || true)"
    p2p_port="$(grep -E '^port=' "$conf_path" | tail -n1 | cut -d= -f2- || true)"

    [ -n "$rpc_user" ] || { echo "ERROR: ${prefix} missing rpcuser in $conf_path" >&2; exit 1; }
    [ -n "$rpc_pass" ] || { echo "ERROR: ${prefix} missing rpcpassword in $conf_path" >&2; exit 1; }
    [ -n "$rpc_port" ] || rpc_port="$default_rpc_port"
    [ -n "$p2p_port" ] || p2p_port="$default_p2p_port"

    echo "${prefix}_RPC_CONF=$conf_path"
    echo "${prefix}_CLI_BIN=$cli_path"
    echo "${prefix}_RPC_USER=$rpc_user"
    echo "${prefix}_RPC_PASS=$rpc_pass"
    echo "${prefix}_RPC_PORT=$rpc_port"
    echo "${prefix}_P2P_PORT=$p2p_port"
}

emit_chain BLC  "$BLC_RPC_CONF"  "$BLC_CLI_BIN"  8772  8773  blakecoin-cli \
    "/root/.blakecoin/blakecoin.conf|/home/blakecoin/.blakecoin/blakecoin.conf|/var/lib/blakecoin/blakecoin.conf|/var/lib/blakecoin-mainnet/blakecoin.conf|/etc/blakecoin/blakecoin.conf" \
    "/opt/blakecoin-current/bin/blakecoin-cli|/usr/local/bin/blakecoin-cli|/usr/bin/blakecoin-cli"
emit_chain BBTC "$BBTC_RPC_CONF" "$BBTC_CLI_BIN" 8243  8356  blakebitcoin-cli \
    "/root/.blakebitcoin/blakebitcoin.conf|/home/blakebitcoin/.blakebitcoin/blakebitcoin.conf|/var/lib/blakebitcoin/blakebitcoin.conf|/var/lib/blakebitcoin-mainnet/blakebitcoin.conf|/etc/blakebitcoin/blakebitcoin.conf" \
    "/opt/blakebitcoin-current/bin/blakebitcoin-cli|/usr/local/bin/blakebitcoin-cli|/usr/bin/blakebitcoin-cli"
emit_chain ELT  "$ELT_RPC_CONF"  "$ELT_CLI_BIN"  6852  6853  electron-cli \
    "/root/.electron/electron.conf|/home/electron/.electron/electron.conf|/var/lib/electron/electron.conf|/var/lib/electron-mainnet/electron.conf|/etc/electron/electron.conf" \
    "/opt/electron-current/bin/electron-cli|/usr/local/bin/electron-cli|/usr/bin/electron-cli"
emit_chain LIT  "$LIT_RPC_CONF"  "$LIT_CLI_BIN"  12000 12007 lithium-cli \
    "/root/.lithium/lithium.conf|/home/lithium/.lithium/lithium.conf|/var/lib/lithium/lithium.conf|/var/lib/lithium-mainnet/lithium.conf|/etc/lithium/lithium.conf" \
    "/opt/lithium-current/bin/lithium-cli|/usr/local/bin/lithium-cli|/usr/bin/lithium-cli"
emit_chain PHO  "$PHO_RPC_CONF"  "$PHO_CLI_BIN"  8984  35556 photon-cli \
    "/root/.photon/photon.conf|/home/photon/.photon/photon.conf|/var/lib/photon/photon.conf|/var/lib/photon-mainnet/photon.conf|/etc/photon/photon.conf" \
    "/opt/photon-current/bin/photon-cli|/usr/local/bin/photon-cli|/usr/bin/photon-cli"
emit_chain UMO  "$UMO_RPC_CONF"  "$UMO_CLI_BIN"  5921  24785 universalmolecule-cli \
    "/root/.universalmolecule/universalmolecule.conf|/root/.universalmol/universalmol.conf|/home/universalmolecule/.universalmolecule/universalmolecule.conf|/home/universalmol/.universalmol/universalmol.conf|/var/lib/universalmolecule/universalmolecule.conf|/var/lib/universalmol/universalmol.conf|/var/lib/universalmolecule-mainnet/universalmolecule.conf|/var/lib/universalmol-mainnet/universalmol.conf|/etc/universalmolecule/universalmolecule.conf|/etc/universalmol/universalmol.conf" \
    "/opt/universalmolecule-current/bin/universalmolecule-cli|/opt/universalmol-current/bin/universalmolecule-cli|/usr/local/bin/universalmolecule-cli|/usr/bin/universalmolecule-cli"
REMOTE
)"

BLC_RPC_CONF_REMOTE="$(extract_discovery_value BLC_RPC_CONF)"
BLC_CLI_BIN_REMOTE="$(extract_discovery_value BLC_CLI_BIN)"
BLC_RPC_USER="$(extract_discovery_value BLC_RPC_USER)"
BLC_RPC_PASS="$(extract_discovery_value BLC_RPC_PASS)"
BLC_RPC_PORT="$(extract_discovery_value BLC_RPC_PORT)"
BLC_P2P_PORT="$(extract_discovery_value BLC_P2P_PORT)"

BBTC_RPC_CONF_REMOTE="$(extract_discovery_value BBTC_RPC_CONF)"
BBTC_CLI_BIN_REMOTE="$(extract_discovery_value BBTC_CLI_BIN)"
BBTC_RPC_USER="$(extract_discovery_value BBTC_RPC_USER)"
BBTC_RPC_PASS="$(extract_discovery_value BBTC_RPC_PASS)"
BBTC_RPC_PORT="$(extract_discovery_value BBTC_RPC_PORT)"

ELT_RPC_CONF_REMOTE="$(extract_discovery_value ELT_RPC_CONF)"
ELT_CLI_BIN_REMOTE="$(extract_discovery_value ELT_CLI_BIN)"
ELT_RPC_USER="$(extract_discovery_value ELT_RPC_USER)"
ELT_RPC_PASS="$(extract_discovery_value ELT_RPC_PASS)"
ELT_RPC_PORT="$(extract_discovery_value ELT_RPC_PORT)"

LIT_RPC_CONF_REMOTE="$(extract_discovery_value LIT_RPC_CONF)"
LIT_CLI_BIN_REMOTE="$(extract_discovery_value LIT_CLI_BIN)"
LIT_RPC_USER="$(extract_discovery_value LIT_RPC_USER)"
LIT_RPC_PASS="$(extract_discovery_value LIT_RPC_PASS)"
LIT_RPC_PORT="$(extract_discovery_value LIT_RPC_PORT)"

PHO_RPC_CONF_REMOTE="$(extract_discovery_value PHO_RPC_CONF)"
PHO_CLI_BIN_REMOTE="$(extract_discovery_value PHO_CLI_BIN)"
PHO_RPC_USER="$(extract_discovery_value PHO_RPC_USER)"
PHO_RPC_PASS="$(extract_discovery_value PHO_RPC_PASS)"
PHO_RPC_PORT="$(extract_discovery_value PHO_RPC_PORT)"

UMO_RPC_CONF_REMOTE="$(extract_discovery_value UMO_RPC_CONF)"
UMO_CLI_BIN_REMOTE="$(extract_discovery_value UMO_CLI_BIN)"
UMO_RPC_USER="$(extract_discovery_value UMO_RPC_USER)"
UMO_RPC_PASS="$(extract_discovery_value UMO_RPC_PASS)"
UMO_RPC_PORT="$(extract_discovery_value UMO_RPC_PORT)"

say "Remote chain discovery"
printf '  Blakecoin         : %s @ rpc %s / p2p %s\n' "$BLC_RPC_CONF_REMOTE" "$BLC_RPC_PORT" "$BLC_P2P_PORT"
printf '  BlakeBitcoin      : %s @ rpc %s\n' "$BBTC_RPC_CONF_REMOTE" "$BBTC_RPC_PORT"
printf '  Electron          : %s @ rpc %s\n' "$ELT_RPC_CONF_REMOTE" "$ELT_RPC_PORT"
printf '  Lithium           : %s @ rpc %s\n' "$LIT_RPC_CONF_REMOTE" "$LIT_RPC_PORT"
printf '  Photon            : %s @ rpc %s\n' "$PHO_RPC_CONF_REMOTE" "$PHO_RPC_PORT"
printf '  UniversalMolecule : %s @ rpc %s\n' "$UMO_RPC_CONF_REMOTE" "$UMO_RPC_PORT"

verify_chain_rpc() {
    local label="$1"
    local cli="$2"
    local conf="$3"
    say "Pre-flight: confirming ${label} RPC is live"
    run_ssh "$(remote_cli_command "$cli" "$conf" getblockchaininfo) >/dev/null"
}

require_nonempty() {
    local label="$1"
    local value="$2"
    if [ -z "$value" ]; then
        echo "ERROR: ${label} is empty; the daemon wallet may still be loading" >&2
        exit 1
    fi
}

verify_parent_mining_ready() {
    local cli="$1"
    local conf="$2"
    say "Pre-flight: confirming Blakecoin getblocktemplate is ready"
    if ! run_ssh "$(remote_cli_command "$cli" "$conf" getblocktemplate '{"rules":["segwit"]}') >/dev/null 2>&1"; then
        echo "ERROR: Blakecoin getblocktemplate is not ready; the parent chain is likely still syncing" >&2
        exit 1
    fi
}

verify_aux_mining_ready() {
    local label="$1"
    local cli="$2"
    local conf="$3"
    local addr="$4"
    say "Pre-flight: confirming ${label} createauxblock is ready"
    if ! run_ssh "$(remote_cli_command "$cli" "$conf" createauxblock "$addr") >/dev/null 2>&1"; then
        echo "ERROR: ${label} createauxblock is not ready; the child chain is likely still syncing" >&2
        exit 1
    fi
}

verify_chain_rpc "Blakecoin" "$BLC_CLI_BIN_REMOTE" "$BLC_RPC_CONF_REMOTE"
verify_chain_rpc "BlakeBitcoin" "$BBTC_CLI_BIN_REMOTE" "$BBTC_RPC_CONF_REMOTE"
verify_chain_rpc "Electron" "$ELT_CLI_BIN_REMOTE" "$ELT_RPC_CONF_REMOTE"
verify_chain_rpc "Lithium" "$LIT_CLI_BIN_REMOTE" "$LIT_RPC_CONF_REMOTE"
verify_chain_rpc "Photon" "$PHO_CLI_BIN_REMOTE" "$PHO_RPC_CONF_REMOTE"
verify_chain_rpc "UniversalMolecule" "$UMO_CLI_BIN_REMOTE" "$UMO_RPC_CONF_REMOTE"

if [ -z "$TRACKER_ADDR" ]; then
    say "Generating Blakecoin pool keep wallet address"
    TRACKER_ADDR="$(
        run_ssh "$(remote_cli_command "$BLC_CLI_BIN_REMOTE" "$BLC_RPC_CONF_REMOTE" getnewaddress '' legacy)"
    )"
fi
require_nonempty "TRACKER_ADDR" "$TRACKER_ADDR"

if [ -z "$POOL_AUX_ADDRESS_BBTC" ]; then
    say "Generating BlakeBitcoin pooled aux payout address"
    POOL_AUX_ADDRESS_BBTC="$(
        generate_remote_aux_address "BlakeBitcoin" "$BBTC_CLI_BIN_REMOTE" "$BBTC_RPC_CONF_REMOTE"
    )"
fi
if [ -z "$POOL_AUX_ADDRESS_ELT" ]; then
    say "Generating Electron pooled aux payout address"
    POOL_AUX_ADDRESS_ELT="$(
        generate_remote_aux_address "Electron" "$ELT_CLI_BIN_REMOTE" "$ELT_RPC_CONF_REMOTE"
    )"
fi
if [ -z "$POOL_AUX_ADDRESS_LIT" ]; then
    say "Generating Lithium pooled aux payout address"
    POOL_AUX_ADDRESS_LIT="$(
        generate_remote_aux_address "Lithium" "$LIT_CLI_BIN_REMOTE" "$LIT_RPC_CONF_REMOTE"
    )"
fi
if [ -z "$POOL_AUX_ADDRESS_PHO" ]; then
    say "Generating Photon pooled aux payout address"
    POOL_AUX_ADDRESS_PHO="$(
        generate_remote_aux_address "Photon" "$PHO_CLI_BIN_REMOTE" "$PHO_RPC_CONF_REMOTE"
    )"
fi
if [ -z "$POOL_AUX_ADDRESS_UMO" ]; then
    say "Generating UniversalMolecule pooled aux payout address"
    POOL_AUX_ADDRESS_UMO="$(
        generate_remote_aux_address "UniversalMolecule" "$UMO_CLI_BIN_REMOTE" "$UMO_RPC_CONF_REMOTE"
    )"
fi

require_nonempty "POOL_AUX_ADDRESS_BBTC" "$POOL_AUX_ADDRESS_BBTC"
require_nonempty "POOL_AUX_ADDRESS_ELT" "$POOL_AUX_ADDRESS_ELT"
require_nonempty "POOL_AUX_ADDRESS_LIT" "$POOL_AUX_ADDRESS_LIT"
require_nonempty "POOL_AUX_ADDRESS_PHO" "$POOL_AUX_ADDRESS_PHO"
require_nonempty "POOL_AUX_ADDRESS_UMO" "$POOL_AUX_ADDRESS_UMO"

verify_parent_mining_ready "$BLC_CLI_BIN_REMOTE" "$BLC_RPC_CONF_REMOTE"
verify_aux_mining_ready "BlakeBitcoin" "$BBTC_CLI_BIN_REMOTE" "$BBTC_RPC_CONF_REMOTE" "$POOL_AUX_ADDRESS_BBTC"
verify_aux_mining_ready "Electron" "$ELT_CLI_BIN_REMOTE" "$ELT_RPC_CONF_REMOTE" "$POOL_AUX_ADDRESS_ELT"
verify_aux_mining_ready "Lithium" "$LIT_CLI_BIN_REMOTE" "$LIT_RPC_CONF_REMOTE" "$POOL_AUX_ADDRESS_LIT"
verify_aux_mining_ready "Photon" "$PHO_CLI_BIN_REMOTE" "$PHO_RPC_CONF_REMOTE" "$POOL_AUX_ADDRESS_PHO"
verify_aux_mining_ready "UniversalMolecule" "$UMO_CLI_BIN_REMOTE" "$UMO_RPC_CONF_REMOTE" "$POOL_AUX_ADDRESS_UMO"

BLC_RPC_URL="http://${BLC_RPC_USER}:${BLC_RPC_PASS}@127.0.0.1:${BLC_RPC_PORT}/"
BBTC_RPC_URL="http://${BBTC_RPC_USER}:${BBTC_RPC_PASS}@127.0.0.1:${BBTC_RPC_PORT}/"
ELT_RPC_URL="http://${ELT_RPC_USER}:${ELT_RPC_PASS}@127.0.0.1:${ELT_RPC_PORT}/"
LIT_RPC_URL="http://${LIT_RPC_USER}:${LIT_RPC_PASS}@127.0.0.1:${LIT_RPC_PORT}/"
PHO_RPC_URL="http://${PHO_RPC_USER}:${PHO_RPC_PASS}@127.0.0.1:${PHO_RPC_PORT}/"
UMO_RPC_URL="http://${UMO_RPC_USER}:${UMO_RPC_PASS}@127.0.0.1:${UMO_RPC_PORT}/"
PARENT_POOL_RPC_URL="http://${POOL_SECRET_USER}:${POOL_SECRET_PASS}@127.0.0.1:${POOL_JSONRPC_PORT}/"
PARENT_GOTWORK_URL="http://${POOL_SECRET_USER}:${POOL_SECRET_PASS}@127.0.0.1:${PROXY_PORT}/"

CHILD_RPC_URLS_JSON="$(
    BBTC_RPC_URL="$BBTC_RPC_URL" \
    ELT_RPC_URL="$ELT_RPC_URL" \
    LIT_RPC_URL="$LIT_RPC_URL" \
    PHO_RPC_URL="$PHO_RPC_URL" \
    UMO_RPC_URL="$UMO_RPC_URL" \
    python3 - <<'PY'
import json
import os
print(json.dumps({
    "BlakeBitcoin": os.environ["BBTC_RPC_URL"],
    "Electron": os.environ["ELT_RPC_URL"],
    "Lithium": os.environ["LIT_RPC_URL"],
    "Photon": os.environ["PHO_RPC_URL"],
    "UniversalMolecule": os.environ["UMO_RPC_URL"],
}, separators=(',', ':')))
PY
)"

AUX_POOL_ADDRESSES_JSON="$(
    POOL_AUX_ADDRESS_BBTC="$POOL_AUX_ADDRESS_BBTC" \
    POOL_AUX_ADDRESS_ELT="$POOL_AUX_ADDRESS_ELT" \
    POOL_AUX_ADDRESS_LIT="$POOL_AUX_ADDRESS_LIT" \
    POOL_AUX_ADDRESS_PHO="$POOL_AUX_ADDRESS_PHO" \
    POOL_AUX_ADDRESS_UMO="$POOL_AUX_ADDRESS_UMO" \
    python3 - <<'PY'
import json
import os
print(json.dumps({
    "BlakeBitcoin": os.environ["POOL_AUX_ADDRESS_BBTC"],
    "Electron": os.environ["POOL_AUX_ADDRESS_ELT"],
    "Lithium": os.environ["POOL_AUX_ADDRESS_LIT"],
    "Photon": os.environ["POOL_AUX_ADDRESS_PHO"],
    "UniversalMolecule": os.environ["POOL_AUX_ADDRESS_UMO"],
}, separators=(',', ':')))
PY
)"

PROXY_ARGS="-w ${PROXY_PORT} -p ${PARENT_POOL_RPC_URL} -s ${PROXY_MERKLE_SIZE}"
PROXY_ARGS+=" -x ${BBTC_RPC_URL} -a ${POOL_AUX_ADDRESS_BBTC}"
PROXY_ARGS+=" -x ${ELT_RPC_URL} -a ${POOL_AUX_ADDRESS_ELT}"
PROXY_ARGS+=" -x ${LIT_RPC_URL} -a ${POOL_AUX_ADDRESS_LIT}"
PROXY_ARGS+=" -x ${PHO_RPC_URL} -a ${POOL_AUX_ADDRESS_PHO}"
PROXY_ARGS+=" -x ${UMO_RPC_URL} -a ${POOL_AUX_ADDRESS_UMO}"

say "Stopping existing pool services before redeploy"
run_ssh "bash -s" <<'REMOTE'
set -euo pipefail
for svc in blakecoin-pool blakecoin-pool-proxy blakecoin-pool-dashboard; do
    systemctl stop "$svc" >/dev/null 2>&1 || true
done
REMOTE

say "Creating ${INSTALL_ROOT} and ${LOG_ROOT}"
run_ssh \
    "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") LOG_ROOT=$(quote_remote "$LOG_ROOT") POOL_USER=$(quote_remote "$POOL_USER") POOL_GROUP=$(quote_remote "$POOL_GROUP") bash -s" <<'REMOTE'
set -euo pipefail

install_root="${INSTALL_ROOT:-/opt/blakecoin-pool}"
log_root="${LOG_ROOT:-/var/log/blakecoin-pool}"
pool_user="${POOL_USER:-blakecoin}"
pool_group="${POOL_GROUP:-blakecoin}"

if ! id "$pool_user" >/dev/null 2>&1; then
    useradd --system --home "$install_root" --shell /usr/sbin/nologin "$pool_user"
fi

mkdir -p "$install_root/eloipool" "$install_root/config" "$install_root/dashboard" "$install_root/bin" "$log_root"
chown -R "$pool_user:$pool_group" "$install_root" "$log_root"
REMOTE

say "Installing runtime packages and dedicated Python venv"
run_ssh \
    "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") POOL_USER=$(quote_remote "$POOL_USER") POOL_GROUP=$(quote_remote "$POOL_GROUP") bash -s" <<'REMOTE'
set -euo pipefail

install_root="${INSTALL_ROOT:-/opt/blakecoin-pool}"
pool_user="${POOL_USER:-blakecoin}"
pool_group="${POOL_GROUP:-blakecoin}"

apt-get update -qq >/dev/null
apt-get install -y -qq git rsync curl python3 python3-venv python3-pip >/dev/null
python3 -m venv --clear "${install_root}/venv"
"${install_root}/venv/bin/pip" install --upgrade pip setuptools wheel >/dev/null
"${install_root}/venv/bin/pip" install Flask Twisted base58 pyasynchat pyasyncore "git+https://github.com/SidGrip/python-bitcoinrpc.git" >/dev/null
chown -R "${pool_user}:${pool_group}" "${install_root}/venv"
REMOTE

say "Rsync eloipool tree"
run_rsync "${ELOIPOOL_SRC}/" "${USER}@${HOST}:${INSTALL_ROOT}/eloipool/"
run_ssh "chown -R ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/eloipool"

say "Rsync dashboard"
run_rsync "${BUNDLE_DIR}/dashboard/" "${USER}@${HOST}:${INSTALL_ROOT}/dashboard/"
run_ssh "chown -R ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/dashboard"

say "Install helper scripts"
run_scp "${BUNDLE_DIR}/coinbaser.py" "${USER}@${HOST}:${INSTALL_ROOT}/bin/coinbaser.py"
run_ssh "chown ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/bin/coinbaser.py && chmod 755 ${INSTALL_ROOT}/bin/coinbaser.py"

say "Using payout settings"
printf '  TrackerAddr        : %s\n' "$TRACKER_ADDR"
printf '  BlakeBitcoin pool  : %s\n' "$POOL_AUX_ADDRESS_BBTC"
printf '  Electron pool      : %s\n' "$POOL_AUX_ADDRESS_ELT"
printf '  Lithium pool       : %s\n' "$POOL_AUX_ADDRESS_LIT"
printf '  Photon pool        : %s\n' "$POOL_AUX_ADDRESS_PHO"
printf '  UniversalMol pool  : %s\n' "$POOL_AUX_ADDRESS_UMO"
printf '  SegWit HRP         : %s\n' "$MINING_KEY_SEGWIT_HRP"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

say "Rendering ${INSTALL_ROOT}/config/config.py"
CONFIG_TMP="$TMPDIR/config.py"
sed \
    -e "s|@@SERVER_NAME@@|$(escape_sed_replacement "$(py_string_literal "$SERVER_NAME")")|g" \
    -e "s|@@TRACKER_ADDR@@|$(escape_sed_replacement "$(py_string_literal "$TRACKER_ADDR")")|g" \
    -e "s|@@COINBASER_CMD@@|$(escape_sed_replacement "$(py_string_literal "${INSTALL_ROOT}/bin/coinbaser.py %d %p")")|g" \
    -e "s|@@PARENT_RPC_URI@@|$(escape_sed_replacement "$(py_string_literal "$BLC_RPC_URL")")|g" \
    -e "s|@@UPSTREAM_P2P_PORT@@|$(escape_sed_replacement "$BLC_P2P_PORT")|g" \
    -e "s|@@POOL_SECRET_USER@@|$(escape_sed_replacement "$(py_string_literal "$POOL_SECRET_USER")")|g" \
    -e "s|@@GOTWORK_URI@@|$(escape_sed_replacement "$(py_string_literal "$PARENT_GOTWORK_URL")")|g" \
    -e "s|@@POOL_JSONRPC_PORT@@|$(escape_sed_replacement "$POOL_JSONRPC_PORT")|g" \
    -e "s|@@STRATUM_PORT@@|$(escape_sed_replacement "$STRATUM_PORT")|g" \
    -e "s|@@SHARE_LOG_PATH@@|$(escape_sed_replacement "$(py_string_literal "${LOG_ROOT}/share-logfile")")|g" \
    -e "s|@@ROTATING_LOG_PATH@@|$(escape_sed_replacement "$(py_string_literal "${LOG_ROOT}/eloipool-rotating.log")")|g" \
    "${BUNDLE_DIR}/config.py.template" > "$CONFIG_TMP"
run_scp "$CONFIG_TMP" "${USER}@${HOST}:${INSTALL_ROOT}/config/config.py"
run_ssh "chown ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/config/config.py && chmod 600 ${INSTALL_ROOT}/config/config.py"

say "Writing ${INSTALL_ROOT}/config/pool.env"
POOL_ENV_TMP="$TMPDIR/pool.env"
: > "$POOL_ENV_TMP"
write_env_line "$POOL_ENV_TMP" PYTHONPATH "${INSTALL_ROOT}/config:${INSTALL_ROOT}/eloipool:${INSTALL_ROOT}/eloipool/vendor:${INSTALL_ROOT}"
write_env_line "$POOL_ENV_TMP" PYTHONUNBUFFERED "1"
write_env_line "$POOL_ENV_TMP" COINBASER_SHARE_LOG "${LOG_ROOT}/share-logfile"
write_env_line "$POOL_ENV_TMP" COINBASER_WINDOW "$COINBASER_WINDOW"
write_env_line "$POOL_ENV_TMP" COINBASER_POOL_KEEP_BPS "$COINBASER_POOL_KEEP_BPS"
write_env_line "$POOL_ENV_TMP" COINBASER_MINING_KEY_SEGWIT_HRP "$MINING_KEY_SEGWIT_HRP"
write_env_line "$POOL_ENV_TMP" COINBASER_DEBUG_LOG "${LOG_ROOT}/coinbaser.jsonl"
write_env_line "$POOL_ENV_TMP" COINBASER_ELOIPOOL_DIR "${INSTALL_ROOT}/eloipool"
run_scp "$POOL_ENV_TMP" "${USER}@${HOST}:${INSTALL_ROOT}/config/pool.env"
run_ssh "chown ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/config/pool.env && chmod 600 ${INSTALL_ROOT}/config/pool.env"

say "Writing ${INSTALL_ROOT}/config/proxy.env"
PROXY_ENV_TMP="$TMPDIR/proxy.env"
: > "$PROXY_ENV_TMP"
write_env_line "$PROXY_ENV_TMP" PROXY_ARGS "$PROXY_ARGS"
write_env_line "$PROXY_ENV_TMP" PROXY_PORT "$PROXY_PORT"
run_scp "$PROXY_ENV_TMP" "${USER}@${HOST}:${INSTALL_ROOT}/config/proxy.env"
run_ssh "chown ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/config/proxy.env && chmod 600 ${INSTALL_ROOT}/config/proxy.env"

say "Writing ${INSTALL_ROOT}/dashboard/dashboard.env"
DASH_ENV_TMP="$TMPDIR/dashboard.env"
: > "$DASH_ENV_TMP"
write_env_line "$DASH_ENV_TMP" DASH_BIND "0.0.0.0:${DASHBOARD_PORT}"
write_env_line "$DASH_ENV_TMP" DASH_RPC_URL "$BLC_RPC_URL"
write_env_line "$DASH_ENV_TMP" DASH_CHILD_RPC_URLS "$CHILD_RPC_URLS_JSON"
write_env_line "$DASH_ENV_TMP" DASH_CHAIN_TICKERS "$DASH_CHAIN_TICKERS"
write_env_line "$DASH_ENV_TMP" DASH_POOL_LOG "${LOG_ROOT}/eloipool.stderr"
write_env_line "$DASH_ENV_TMP" DASH_PROXY_LOG "${LOG_ROOT}/merged-mine-proxy.log"
write_env_line "$DASH_ENV_TMP" DASH_SHARE_LOG "${LOG_ROOT}/share-logfile"
write_env_line "$DASH_ENV_TMP" DASH_TRACKER_ADDR "$TRACKER_ADDR"
write_env_line "$DASH_ENV_TMP" DASH_STRATUM_HOST "$PUBLIC_HOST"
write_env_line "$DASH_ENV_TMP" DASH_STRATUM_PORT "$STRATUM_PORT"
write_env_line "$DASH_ENV_TMP" DASH_HEADER_TITLE "$DASH_HEADER_TITLE"
write_env_line "$DASH_ENV_TMP" DASH_HEADER_SUBTITLE "$DASH_HEADER_SUBTITLE"
write_env_line "$DASH_ENV_TMP" DASH_MINING_KEY_SEGWIT_HRP "$MINING_KEY_SEGWIT_HRP"
write_env_line "$DASH_ENV_TMP" DASH_MINING_KEY_V2_COIN_HRPS "$DASH_MINING_KEY_V2_COIN_HRPS"
write_env_line "$DASH_ENV_TMP" DASH_COINBASER_DEBUG_LOG "${LOG_ROOT}/coinbaser.jsonl"
write_env_line "$DASH_ENV_TMP" DASH_AUX_POOL_ADDRESSES "$AUX_POOL_ADDRESSES_JSON"
write_env_line "$DASH_ENV_TMP" DASH_AUX_PAYOUT_MODE "pool"
write_env_line "$DASH_ENV_TMP" DASH_ELOIPOOL_PATH "${INSTALL_ROOT}/eloipool"
write_env_line "$DASH_ENV_TMP" DASH_COINBASER "${INSTALL_ROOT}/bin/coinbaser.py"
run_scp "$DASH_ENV_TMP" "${USER}@${HOST}:${INSTALL_ROOT}/dashboard/dashboard.env"
run_ssh "chown ${POOL_USER}:${POOL_GROUP} ${INSTALL_ROOT}/dashboard/dashboard.env && chmod 600 ${INSTALL_ROOT}/dashboard/dashboard.env"

say "Rendering systemd units"
POOL_UNIT_TMP="$TMPDIR/blakecoin-pool.service"
PROXY_UNIT_TMP="$TMPDIR/blakecoin-pool-proxy.service"
DASH_UNIT_TMP="$TMPDIR/blakecoin-pool-dashboard.service"
render_unit_template "${BUNDLE_DIR}/systemd/blakecoin-pool.service" "$POOL_UNIT_TMP"
render_unit_template "${BUNDLE_DIR}/systemd/blakecoin-pool-proxy.service" "$PROXY_UNIT_TMP"
render_unit_template "${BUNDLE_DIR}/systemd/blakecoin-pool-dashboard.service" "$DASH_UNIT_TMP"

say "Installing systemd units"
run_scp "$POOL_UNIT_TMP" "${USER}@${HOST}:/etc/systemd/system/blakecoin-pool.service"
run_scp "$PROXY_UNIT_TMP" "${USER}@${HOST}:/etc/systemd/system/blakecoin-pool-proxy.service"
run_scp "$DASH_UNIT_TMP" "${USER}@${HOST}:/etc/systemd/system/blakecoin-pool-dashboard.service"
run_ssh "systemctl daemon-reload && systemctl enable blakecoin-pool blakecoin-pool-proxy blakecoin-pool-dashboard"

if [ "$UFW_ENABLE" = "1" ]; then
    say "Opening firewall ports when ufw is present"
    run_ssh "command -v ufw >/dev/null 2>&1 && { ufw allow ${STRATUM_PORT}/tcp >/dev/null || true; ufw allow ${DASHBOARD_PORT}/tcp >/dev/null || true; } || true"
fi

say "Restarting services"
run_ssh "systemctl restart blakecoin-pool && sleep 2 && systemctl restart blakecoin-pool-proxy && sleep 2 && systemctl restart blakecoin-pool-dashboard && sleep 1"
run_ssh "systemctl is-active blakecoin-pool && systemctl is-active blakecoin-pool-proxy && systemctl is-active blakecoin-pool-dashboard"

say "Checking dashboard health"
run_ssh "curl -fsS http://127.0.0.1:${DASHBOARD_PORT}/api/state >/dev/null"

say "DONE"
echo
echo "  Stratum  : stratum+tcp://${PUBLIC_HOST}:${STRATUM_PORT}"
echo "  Dashboard: http://${PUBLIC_HOST}:${DASHBOARD_PORT}/"
echo "  Tracker  : ${TRACKER_ADDR}"
echo "  BLC RPC  : ${BLC_RPC_CONF_REMOTE}"
echo
echo "  Aux pool addresses:"
echo "    BlakeBitcoin      ${POOL_AUX_ADDRESS_BBTC}"
echo "    Electron          ${POOL_AUX_ADDRESS_ELT}"
echo "    Lithium           ${POOL_AUX_ADDRESS_LIT}"
echo "    Photon            ${POOL_AUX_ADDRESS_PHO}"
echo "    UniversalMolecule ${POOL_AUX_ADDRESS_UMO}"
echo
echo "  Logs: ssh ${USER}@${HOST} 'journalctl -u blakecoin-pool -u blakecoin-pool-proxy -u blakecoin-pool-dashboard -f'"
