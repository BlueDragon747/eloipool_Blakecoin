#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# BlakeStream Eloipool 15.21 full merged-mining stack deployer
#
# Purpose:
#   - install the staged Eloipool tree from Blakestream-Eliopool-15.21
#   - install daemons for all 6 chains
#   - run a private, isolated merged-mining stack:
#       Blakecoin parent + 5 AuxPoW children
#       merged-mine-proxy
#       eloipool
#       read-only dashboard
#       single-core CPU miner
#
# Defaults:
#   host                : first CLI arg
#   user                : root
#   password            : third CLI arg if password auth is needed
#   install root        : /opt/blakestream-stack
#   data root           : /var/lib/blakestream-stack
#   log root            : /var/log/blakestream-stack
#   dashboard port      : 18081
#   stratum port        : 3334
#   miner username      : bare V2 mining key
#
# Safety:
#   Network mode defaults to mainnet. Testnet and regtest are explicit
#   overrides. Existing datadirs are preserved across redeploys.
# -----------------------------------------------------------------------------

RUN_LOCAL=0
HOST=""
USER="root"
PASS=""
MODE_FLAG=""

usage() {
    echo "Usage:"
    echo "  bash deploy-full-stack.sh <host> [user] [password]"
    echo "  bash deploy-full-stack.sh -local [--bootstrap]"
    echo "  bash deploy-full-stack.sh -pull [--bootstrap]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -local|--local)
            RUN_LOCAL=1
            MODE_FLAG="local"
            shift
            ;;
        -pull|--pull)
            RUN_LOCAL=1
            MODE_FLAG="pull"
            shift
            ;;
        --bootstrap)
            DOWNLOAD_BOOTSTRAP=true
            shift
            ;;
        --no-bootstrap)
            DOWNLOAD_BOOTSTRAP=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            if [ -z "$HOST" ]; then
                HOST="$1"
            elif [ "$USER" = "root" ]; then
                USER="$1"
            elif [ -z "$PASS" ]; then
                PASS="$1"
            else
                echo "Unexpected argument: $1" >&2
                usage
                exit 1
            fi
            shift
            ;;
    esac
done

if [ "$RUN_LOCAL" = "1" ]; then
    HOST="${HOST:-127.0.0.1}"
    USER="${USER:-$(id -un)}"
else
    if [ -z "$HOST" ]; then
        usage
        exit 1
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUNDLE_DIR="${SCRIPT_DIR}"
NETWORK_MODE="${NETWORK_MODE:-mainnet}"
NETWORK_TAG="${NETWORK_MODE}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/blakestream-${NETWORK_TAG}}"
DATA_ROOT="${DATA_ROOT:-/var/lib/blakestream-${NETWORK_TAG}}"
LOG_ROOT="${LOG_ROOT:-/var/log/blakestream-${NETWORK_TAG}}"
RUN_USER="${RUN_USER:-blakestream}"
RUN_GROUP="${RUN_GROUP:-blakestream}"
if [ -z "${PUBLIC_HOST:-}" ]; then
    if [ "$RUN_LOCAL" = "1" ]; then
        PUBLIC_HOST="$(ip route get 1.1.1.1 2>/dev/null | awk 'NR==1{for(i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
        PUBLIC_HOST="${PUBLIC_HOST:-$HOST}"
    else
        PUBLIC_HOST="$HOST"
    fi
fi
STRATUM_PORT="${STRATUM_PORT:-3334}"
DASHBOARD_PORT="${DASHBOARD_PORT:-18081}"
POOL_JSONRPC_PORT="${POOL_JSONRPC_PORT:-19334}"
PROXY_PORT="${PROXY_PORT:-19335}"
POOL_SECRET_USER="${POOL_SECRET_USER:-auxpow}"
POOL_SECRET_PASS="${POOL_SECRET_PASS:-auxpow}"
NODE_RPC_USER="${NODE_RPC_USER:-blakestream}"
NODE_RPC_PASS="${NODE_RPC_PASS:-blakestream-${NETWORK_TAG}}"
MERKLE_SIZE="${MERKLE_SIZE:-16}"
DAEMON_INSTALL_MODE="${DAEMON_INSTALL_MODE:-${MODE_FLAG:-local}}"
DOWNLOAD_BOOTSTRAP="${DOWNLOAD_BOOTSTRAP:-false}"
BOOTSTRAP_BASE_URL="${BOOTSTRAP_BASE_URL:-https://bootstrap.blakestream.io}"
USE_EXPLORER_PEERS="${USE_EXPLORER_PEERS:-true}"
REPO_SYNC_ROOT="${REPO_SYNC_ROOT:-$ROOT}"
REMOTE_BUILD_JOBS="${REMOTE_BUILD_JOBS:-$(nproc)}"
REMOTE_DB4_PREFIX="${REMOTE_DB4_PREFIX:-${INSTALL_ROOT}/db4}"
DAEMON_IMAGE_NAMESPACE="${DAEMON_IMAGE_NAMESPACE:-sidgrip}"
DAEMON_IMAGE_TAG="${DAEMON_IMAGE_TAG:-15.21}"
MINER_PRIVATE_KEY="${MINER_PRIVATE_KEY:-b23202ffe429488512ad371474754fc74e832076e4a56f5515012e329a3edce4}"
MINER_USERNAME="${MINER_USERNAME:-1a8ef9c714af41ba978af9c53965e7eb743cce5b}"
MINER_PAYOUT_ADDRESS="${MINER_PAYOUT_ADDRESS:-}"
SOLVER_SOURCE="${SOLVER_SOURCE:-${BUNDLE_DIR}/solve_blake_header.c}"

SOURCE_MAP=(
    "blakecoin|BLC|Blakecoin|blakecoind|blakecoin-cli|blakecoin-tx|https://github.com/BlueDragon747/Blakecoin.git|master|blakecoin"
    "blakebitcoin|BBTC|BlakeBitcoin|blakebitcoind|blakebitcoin-cli|blakebitcoin-tx|https://github.com/BlakeBitcoin/BlakeBitcoin.git|master|blakebitcoin"
    "electron|ELT|Electron|electrond|electron-cli|electron-tx|https://github.com/BlueDragon747/Electron-ELT.git|master|electron"
    "lithium|LIT|Lithium|lithiumd|lithium-cli|lithium-tx|https://github.com/BlueDragon747/lithium.git|master|lithium"
    "photon|PHO|Photon|photond|photon-cli|photon-tx|https://github.com/BlueDragon747/photon.git|master|photon"
    "universalmol|UMO|UniversalMolecule|universalmoleculed|universalmolecule-cli|universalmolecule-tx|https://github.com/BlueDragon747/universalmol.git|master|universalmolecule"
)

SSH_OPTS=(
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=10
)

say() {
    printf '\033[1;36m==>\033[0m %s\n' "$*"
}

die() {
    printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
    exit 1
}

quote_remote() {
    printf "%q" "$1"
}

run_ssh() {
    if [ "$RUN_LOCAL" = "1" ]; then
        if [ "$#" -gt 0 ]; then
            bash -lc "$*"
        else
            bash -s
        fi
    elif [ -n "$PASS" ]; then
        sshpass -p "$PASS" ssh "${SSH_OPTS[@]}" "${USER}@${HOST}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "${USER}@${HOST}" "$@"
    fi
}

local_dest_path() {
    case "$1" in
        *:*)
            printf '%s' "${1#*:}"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

run_scp() {
    if [ "$RUN_LOCAL" = "1" ]; then
        local args=("$@")
        local dest_index=$((${#args[@]} - 1))
        local dest
        dest="$(local_dest_path "${args[$dest_index]}")"
        mkdir -p "$dest"
        cp "${args[@]:0:$dest_index}" "$dest"
    elif [ -n "$PASS" ]; then
        sshpass -p "$PASS" scp "${SSH_OPTS[@]}" "$@"
    else
        scp "${SSH_OPTS[@]}" "$@"
    fi
}

run_rsync() {
    if [ "$RUN_LOCAL" = "1" ]; then
        local args=("$@")
        local dest_index=$((${#args[@]} - 1))
        args[$dest_index]="$(local_dest_path "${args[$dest_index]}")"
        rsync -az --delete "${args[@]}"
    elif [ -n "$PASS" ]; then
        sshpass -p "$PASS" rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "$@"
    else
        rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "$@"
    fi
}

require_local_file() {
    local path="$1"
    [ -e "$path" ] || die "missing required local file: $path"
}

RPC_PORT_BLC=29332
P2P_PORT_BLC=29777
RPC_PORT_BLC_PEER=29333
P2P_PORT_BLC_PEER=29778
RPC_PORT_BBTC=29112
P2P_PORT_BBTC=29113
RPC_PORT_BBTC_PEER=29114
P2P_PORT_BBTC_PEER=29115
RPC_PORT_ELT=26852
P2P_PORT_ELT=26853
RPC_PORT_ELT_PEER=26854
P2P_PORT_ELT_PEER=26855
RPC_PORT_LIT=32004
P2P_PORT_LIT=32000
RPC_PORT_LIT_PEER=32005
P2P_PORT_LIT_PEER=32001
RPC_PORT_PHO=28998
P2P_PORT_PHO=28992
RPC_PORT_PHO_PEER=28999
P2P_PORT_PHO_PEER=28993
RPC_PORT_UMO=29738
P2P_PORT_UMO=29449
RPC_PORT_UMO_PEER=29739
P2P_PORT_UMO_PEER=29450

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

STAGE_ROOT="$TMPDIR/stage"
mkdir -p "$STAGE_ROOT/bin" "$STAGE_ROOT/systemd" "$STAGE_ROOT/config" "$STAGE_ROOT/dashboard" "$STAGE_ROOT/share"

require_local_file "$BUNDLE_DIR/coinbaser.py"
require_local_file "$BUNDLE_DIR/cpu_miner.py"
require_local_file "$ROOT/blake8.py"
require_local_file "$ROOT/mining_key.py"
require_local_file "$SOLVER_SOURCE"

case "$NETWORK_MODE" in
    mainnet)
        DAEMON_NETWORK_FLAG=""
        NETWORK_DISPLAY="mainnet"
        BLC_HRP="blc"
        BBTC_HRP="bbtc"
        ELT_HRP="elt"
        LIT_HRP="lit"
        PHO_HRP="pho"
        UMO_HRP="umo"
        POOL_AUX_ADDRESS_TYPE="${POOL_AUX_ADDRESS_TYPE:-legacy}"
        DASH_HEADER_TITLE="${DASH_HEADER_TITLE:-BLC Mainnet Pool}"
        DASH_HEADER_SUBTITLE="${DASH_HEADER_SUBTITLE:-Blakestream-Eliopool-15.21-mainnet}"
        ENABLE_CPU_MINER=false
        ;;
    testnet)
        DAEMON_NETWORK_FLAG="-testnet"
        NETWORK_DISPLAY="testnet"
        BLC_HRP="tblc"
        BBTC_HRP="tbbtc"
        ELT_HRP="telt"
        LIT_HRP="tlit"
        PHO_HRP="tpho"
        UMO_HRP="tumo"
        POOL_AUX_ADDRESS_TYPE="${POOL_AUX_ADDRESS_TYPE:-bech32}"
        DASH_HEADER_TITLE="${DASH_HEADER_TITLE:-BLC Testnet Pool}"
        DASH_HEADER_SUBTITLE="${DASH_HEADER_SUBTITLE:-Blakestream-Eliopool-15.21-testnet}"
        ENABLE_CPU_MINER=true
        ;;
    regtest)
        DAEMON_NETWORK_FLAG="-regtest"
        NETWORK_DISPLAY="regtest"
        BLC_HRP="rblc"
        BBTC_HRP="rbbtc"
        ELT_HRP="relt"
        LIT_HRP="rlit"
        PHO_HRP="rpho"
        UMO_HRP="rumo"
        POOL_AUX_ADDRESS_TYPE="${POOL_AUX_ADDRESS_TYPE:-bech32}"
        DASH_HEADER_TITLE="${DASH_HEADER_TITLE:-BLC Regtest Pool}"
        DASH_HEADER_SUBTITLE="${DASH_HEADER_SUBTITLE:-Blakestream-Eliopool-15.21-regtest}"
        ENABLE_CPU_MINER=true
        ;;
    *)
        die "unsupported NETWORK_MODE=${NETWORK_MODE} (expected mainnet, testnet, or regtest)"
        ;;
esac

case "$DAEMON_INSTALL_MODE" in
    local|pull)
        ;;
    *)
        die "unsupported DAEMON_INSTALL_MODE=${DAEMON_INSTALL_MODE} (expected local or pull)"
        ;;
esac

say "Pre-flight: checking SSH reachability"
run_ssh "echo ok && uname -a && whoami"

say "Detecting existing BlakeStream runtimes on the target"
run_ssh "bash -s" <<'REMOTE'
set -euo pipefail
for row in \
  "BLC|blakecoind|blakestream-testnet-blakecoin.service|blakestream-testnet-blakecoin|blakecoin" \
  "BBTC|blakebitcoind|blakestream-testnet-blakebitcoin.service|blakestream-testnet-blakebitcoin|blakebitcoin" \
  "ELT|electrond|blakestream-testnet-electron.service|blakestream-testnet-electron|electron" \
  "LIT|lithiumd|blakestream-testnet-lithium.service|blakestream-testnet-lithium|lithium" \
  "PHO|photond|blakestream-testnet-photon.service|blakestream-testnet-photon|photon" \
  "UMO|universalmoleculed|blakestream-testnet-universalmol.service|blakestream-testnet-universalmol|universalmolecule"
do
    IFS='|' read -r ticker daemon_name unit_name primary_container alt_container <<EOF
$row
EOF
    status=()
    unit_state="$(systemctl is-active "${unit_name}" 2>/dev/null || true)"
    if [ -n "${unit_state}" ]; then
        status+=("systemd:${unit_state}")
    fi
    if pgrep -x "${daemon_name}" >/dev/null 2>&1; then
        status+=("process")
    fi
    if command -v docker >/dev/null 2>&1; then
        if docker ps -a --format '{{.Names}}' | grep -Eq "^(${primary_container}|${alt_container}|blakestream-${alt_container})$"; then
            status+=("docker")
        fi
    fi
    if [ "${#status[@]}" -gt 0 ]; then
        printf '%s detected using currently running daemons (%s)\n' "${ticker}" "$(IFS=', '; echo "${status[*]}")"
    else
        printf '%s not detected\n' "${ticker}"
    fi
done
REMOTE

say "Preparing local staging payload"
run_rsync "$BUNDLE_DIR/eloipool/" "$STAGE_ROOT/eloipool/"
run_rsync "$BUNDLE_DIR/dashboard/" "$STAGE_ROOT/dashboard/"
cp "$BUNDLE_DIR/coinbaser.py" "$STAGE_ROOT/bin/coinbaser.py"
cp "$BUNDLE_DIR/cpu_miner.py" "$STAGE_ROOT/bin/cpu_miner.py"
cp "$ROOT/blake8.py" "$STAGE_ROOT/blake8.py"
cp "$ROOT/mining_key.py" "$STAGE_ROOT/mining_key.py"
cp "$SOLVER_SOURCE" "$STAGE_ROOT/share/solve_blake_header.c"
chmod 755 "$STAGE_ROOT/bin/"*

cat > "$STAGE_ROOT/bin/rpc_call.py" <<'PY'
#!/usr/bin/env python3
import base64
import json
import os
import sys
import urllib.request

if len(sys.argv) < 3:
    print("usage: rpc_call.py <rpcport> <method> [params...]", file=sys.stderr)
    sys.exit(1)

rpc_port = sys.argv[1]
method = sys.argv[2]
raw_params = sys.argv[3:]
rpc_user = os.environ.get("RPC_USER", "blakestream")
rpc_pass = os.environ.get("RPC_PASSWORD", "blakestream-testnet")

def parse_value(raw):
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if raw.startswith("json:"):
        return json.loads(raw[5:])
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw

params = [parse_value(v) for v in raw_params]
body = json.dumps({"jsonrpc": "1.0", "id": "rpc", "method": method, "params": params}).encode()
auth = base64.b64encode(f"{rpc_user}:{rpc_pass}".encode()).decode()
req = urllib.request.Request(
    f"http://127.0.0.1:{rpc_port}/",
    data=body,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth}",
    },
)

with urllib.request.urlopen(req, timeout=20) as resp:
    payload = json.loads(resp.read())

if payload.get("error"):
    print(json.dumps(payload["error"]), file=sys.stderr)
    sys.exit(2)

result = payload.get("result")
if isinstance(result, str):
    print(result)
elif result is None:
    pass
else:
    print(json.dumps(result, sort_keys=True))
PY
chmod 755 "$STAGE_ROOT/bin/rpc_call.py"

render_daemon_unit() {
    local unit_path="$1"
    local display_name="$2"
    local daemon_name="$3"
    local rpc_port="$4"
    local p2p_port="$5"
    local coin_dir="$6"
    local data_suffix="$7"
    local peer_port="$8"
    local config_name="$9"
    local datadir="${DATA_ROOT}/${coin_dir}${data_suffix}"
    cat > "$unit_path" <<EOF
[Unit]
Description=BlakeStream ${NETWORK_DISPLAY} ${display_name} daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_ROOT}
Environment=LD_LIBRARY_PATH=${INSTALL_ROOT}/lib
Environment=RPC_USER=${NODE_RPC_USER}
Environment=RPC_PASSWORD=${NODE_RPC_PASS}
ExecStart=${INSTALL_ROOT}/bin/${daemon_name} ${DAEMON_NETWORK_FLAG} -conf=${datadir}/${config_name} -datadir=${datadir}
ExecStop=-/usr/bin/env RPC_USER=${NODE_RPC_USER} RPC_PASSWORD=${NODE_RPC_PASS} ${INSTALL_ROOT}/bin/rpc_call.py ${rpc_port} stop
StandardOutput=append:${LOG_ROOT}/${coin_dir}${data_suffix}.stdout
StandardError=append:${LOG_ROOT}/${coin_dir}${data_suffix}.stderr
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF
}

render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-blakecoin.service" "Blakecoin" "blakecoind" "$RPC_PORT_BLC" "$P2P_PORT_BLC" "blakecoin" "" "$P2P_PORT_BLC_PEER" "blakecoin.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-blakecoin-peer.service" "Blakecoin peer" "blakecoind" "$RPC_PORT_BLC_PEER" "$P2P_PORT_BLC_PEER" "blakecoin" "-peer" "$P2P_PORT_BLC" "blakecoin.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-blakebitcoin.service" "BlakeBitcoin" "blakebitcoind" "$RPC_PORT_BBTC" "$P2P_PORT_BBTC" "blakebitcoin" "" "$P2P_PORT_BBTC_PEER" "blakebitcoin.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-blakebitcoin-peer.service" "BlakeBitcoin peer" "blakebitcoind" "$RPC_PORT_BBTC_PEER" "$P2P_PORT_BBTC_PEER" "blakebitcoin" "-peer" "$P2P_PORT_BBTC" "blakebitcoin.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-electron.service" "Electron" "electrond" "$RPC_PORT_ELT" "$P2P_PORT_ELT" "electron" "" "$P2P_PORT_ELT_PEER" "electron.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-electron-peer.service" "Electron peer" "electrond" "$RPC_PORT_ELT_PEER" "$P2P_PORT_ELT_PEER" "electron" "-peer" "$P2P_PORT_ELT" "electron.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-lithium.service" "Lithium" "lithiumd" "$RPC_PORT_LIT" "$P2P_PORT_LIT" "lithium" "" "$P2P_PORT_LIT_PEER" "lithium.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-lithium-peer.service" "Lithium peer" "lithiumd" "$RPC_PORT_LIT_PEER" "$P2P_PORT_LIT_PEER" "lithium" "-peer" "$P2P_PORT_LIT" "lithium.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-photon.service" "Photon" "photond" "$RPC_PORT_PHO" "$P2P_PORT_PHO" "photon" "" "$P2P_PORT_PHO_PEER" "photon.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-photon-peer.service" "Photon peer" "photond" "$RPC_PORT_PHO_PEER" "$P2P_PORT_PHO_PEER" "photon" "-peer" "$P2P_PORT_PHO" "photon.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-universalmol.service" "UniversalMolecule" "universalmoleculed" "$RPC_PORT_UMO" "$P2P_PORT_UMO" "universalmol" "" "$P2P_PORT_UMO_PEER" "universalmolecule.conf"
render_daemon_unit "$STAGE_ROOT/systemd/blakestream-testnet-universalmol-peer.service" "UniversalMolecule peer" "universalmoleculed" "$RPC_PORT_UMO_PEER" "$P2P_PORT_UMO_PEER" "universalmol" "-peer" "$P2P_PORT_UMO" "universalmolecule.conf"

say "Stopping any existing ${NETWORK_DISPLAY} BlakeStream services before redeploy"
run_ssh \
    "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") DATA_ROOT=$(quote_remote "$DATA_ROOT") LOG_ROOT=$(quote_remote "$LOG_ROOT") bash -s" <<'REMOTE'
set -euo pipefail

(systemctl list-unit-files 'blakestream-testnet-*' --no-legend 2>/dev/null || true) | awk '{print $1}' | while read -r unit; do
    [ -n "$unit" ] || continue
    systemctl stop --no-block "$unit" >/dev/null 2>&1 || true
    systemctl kill --signal=SIGKILL "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || true
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
done
if command -v docker >/dev/null 2>&1; then
    docker ps -a --format '{{.Names}}' | grep '^blakestream-testnet-' | xargs -r docker rm -f >/dev/null 2>&1 || true
fi
systemctl daemon-reload
REMOTE

say "Installing VPS dependencies for ${DAEMON_INSTALL_MODE} mode"
run_ssh "DAEMON_INSTALL_MODE=$(quote_remote "$DAEMON_INSTALL_MODE") bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl wget rsync python3 python3-venv python3-pip g++ ca-certificates libboost-filesystem1.83.0 libboost-program-options1.83.0 libboost-thread1.83.0 libboost-chrono1.83.0 libminiupnpc17 libevent-2.1-7t64 libevent-pthreads-2.1-7t64 >/dev/null
if [ "${DAEMON_INSTALL_MODE}" = "local" ]; then
    apt-get install -y -qq build-essential autoconf automake libtool pkg-config git libssl-dev libevent-dev libminiupnpc-dev libboost-all-dev >/dev/null
elif [ "${DAEMON_INSTALL_MODE}" = "pull" ]; then
    apt-get install -y -qq docker.io git >/dev/null
    systemctl enable --now docker >/dev/null 2>&1 || true
fi
REMOTE

say "Creating remote install/data/log roots"
run_ssh \
    "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") DATA_ROOT=$(quote_remote "$DATA_ROOT") LOG_ROOT=$(quote_remote "$LOG_ROOT") RUN_USER=$(quote_remote "$RUN_USER") RUN_GROUP=$(quote_remote "$RUN_GROUP") bash -s" <<'REMOTE'
set -euo pipefail

if ! id "${RUN_USER}" >/dev/null 2>&1; then
    useradd --system --home "${INSTALL_ROOT}" --shell /usr/sbin/nologin "${RUN_USER}"
fi

mkdir -p "${INSTALL_ROOT}" "${DATA_ROOT}" "${LOG_ROOT}"
chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_ROOT}" "${DATA_ROOT}" "${LOG_ROOT}"
REMOTE

say "Uploading the staged stack payload"
run_ssh "mkdir -p ${INSTALL_ROOT}/bin ${INSTALL_ROOT}/share ${INSTALL_ROOT}/eloipool ${INSTALL_ROOT}/dashboard"
run_rsync "$STAGE_ROOT/eloipool/" "${USER}@${HOST}:${INSTALL_ROOT}/eloipool/"
run_rsync "$STAGE_ROOT/dashboard/" "${USER}@${HOST}:${INSTALL_ROOT}/dashboard/"
run_scp \
    "$STAGE_ROOT/bin/coinbaser.py" \
    "$STAGE_ROOT/bin/cpu_miner.py" \
    "$STAGE_ROOT/bin/rpc_call.py" \
    "${USER}@${HOST}:${INSTALL_ROOT}/bin/"
run_scp "$STAGE_ROOT/share/solve_blake_header.c" "${USER}@${HOST}:${INSTALL_ROOT}/share/"
run_ssh "chown -R ${RUN_USER}:${RUN_GROUP} ${INSTALL_ROOT}"

if [ "$RUN_LOCAL" != "1" ]; then
    say "Syncing the local Eliopool repo to the VPS"
    run_rsync \
        --exclude '.git' \
        --exclude '.pytest_cache' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude 'vendor/**/__pycache__' \
        "$ROOT/" "${USER}@${HOST}:${REPO_SYNC_ROOT}/"
fi

if [ "$DAEMON_INSTALL_MODE" = "local" ]; then
    say "Building all six daemon binaries from source on the VPS"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") \
         REPO_SYNC_ROOT=$(quote_remote "$REPO_SYNC_ROOT") \
         REMOTE_BUILD_JOBS=$(quote_remote "$REMOTE_BUILD_JOBS") \
         REMOTE_DB4_PREFIX=$(quote_remote "$REMOTE_DB4_PREFIX") \
         RUN_USER=$(quote_remote "$RUN_USER") \
         RUN_GROUP=$(quote_remote "$RUN_GROUP") \
         bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

SOURCE_ROOT="${INSTALL_ROOT}/source"
mkdir -p "${SOURCE_ROOT}" "${INSTALL_ROOT}/bin"

build_db4() {
    if [ -f "${REMOTE_DB4_PREFIX}/lib/libdb_cxx-4.8.a" ] || [ -f "${REMOTE_DB4_PREFIX}/lib/libdb_cxx.a" ]; then
        return 0
    fi
    workdir="$(mktemp -d)"
    trap 'rm -rf "${workdir}"' RETURN
    (
        cd "${workdir}"
        wget -q http://download.oracle.com/berkeley-db/db-4.8.30.NC.tar.gz
        echo "12edc0df75bf9abd7f82f821795bcee50f42cb2e5f76a6a281b85732798364ef  db-4.8.30.NC.tar.gz" | sha256sum -c - >/dev/null
        tar xzf db-4.8.30.NC.tar.gz
        sed -i 's/__atomic_compare_exchange/__atomic_compare_exchange_db/g' db-4.8.30.NC/dbinc/atomic.h
        cd db-4.8.30.NC/build_unix
        ../dist/configure --enable-cxx --disable-shared --with-pic --prefix="${REMOTE_DB4_PREFIX}" >/dev/null
        make -j"${REMOTE_BUILD_JOBS}" >/dev/null
        make install >/dev/null
    )
}

build_db4

solver_src=""
while IFS='|' read -r key ticker label daemon_name cli_name tx_name repo_url repo_branch image_repo; do
    [ -n "${key}" ] || continue
    if [ -x "${INSTALL_ROOT}/bin/${daemon_name}" ] && [ -x "${INSTALL_ROOT}/bin/${cli_name}" ] && [ -x "${INSTALL_ROOT}/bin/${tx_name}" ]; then
        if [ "${key}" = "blakecoin" ]; then
            solver_src="${SOURCE_ROOT}/${key}"
        fi
        continue
    fi
    local_src="${SOURCE_ROOT}/${key}"
    rm -rf "${local_src}"
    git clone --depth 1 -b "${repo_branch}" "${repo_url}" "${local_src}" >/dev/null 2>&1
    (
        cd "${local_src}"
        ./autogen.sh >/dev/null
        ./configure \
            --without-gui \
            --disable-tests \
            --disable-bench \
            --with-incompatible-bdb \
            CPPFLAGS="-I${REMOTE_DB4_PREFIX}/include" \
            LDFLAGS="-L${REMOTE_DB4_PREFIX}/lib" >/dev/null
        make -j"${REMOTE_BUILD_JOBS}" >/dev/null
    )
    install -m 755 "${local_src}/src/${daemon_name}" "${INSTALL_ROOT}/bin/${daemon_name}"
    install -m 755 "${local_src}/src/${cli_name}" "${INSTALL_ROOT}/bin/${cli_name}"
    install -m 755 "${local_src}/src/${tx_name}" "${INSTALL_ROOT}/bin/${tx_name}"
    if [ "${key}" = "blakecoin" ]; then
        solver_src="${local_src}"
    fi
done <<'MAP'
blakecoin|BLC|Blakecoin|blakecoind|blakecoin-cli|blakecoin-tx|https://github.com/BlueDragon747/Blakecoin.git|master|blakecoin
blakebitcoin|BBTC|BlakeBitcoin|blakebitcoind|blakebitcoin-cli|blakebitcoin-tx|https://github.com/BlakeBitcoin/BlakeBitcoin.git|master|blakebitcoin
electron|ELT|Electron|electrond|electron-cli|electron-tx|https://github.com/BlueDragon747/Electron-ELT.git|master|electron
lithium|LIT|Lithium|lithiumd|lithium-cli|lithium-tx|https://github.com/BlueDragon747/lithium.git|master|lithium
photon|PHO|Photon|photond|photon-cli|photon-tx|https://github.com/BlueDragon747/photon.git|master|photon
universalmol|UMO|UniversalMolecule|universalmoleculed|universalmolecule-cli|universalmolecule-tx|https://github.com/BlueDragon747/universalmol.git|master|universalmolecule
MAP

if [ ! -x "${INSTALL_ROOT}/bin/solve_blake_header" ]; then
    if [ -z "${solver_src}" ]; then
        solver_src="${SOURCE_ROOT}/blakecoin"
    fi
    g++ -O3 \
        -I "${solver_src}/src/crypto/blake" \
        -I "${solver_src}/src" \
        -o "${INSTALL_ROOT}/bin/solve_blake_header" \
        "${INSTALL_ROOT}/share/solve_blake_header.c" \
        "${solver_src}/src/crypto/blake/blake.cpp"
fi

chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_ROOT}/bin" "${SOURCE_ROOT}" "${REMOTE_DB4_PREFIX}"
REMOTE
elif [ "$DAEMON_INSTALL_MODE" = "pull" ]; then
    say "Pulling daemon binaries from Docker Hub on the VPS"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") \
         REMOTE_BUILD_JOBS=$(quote_remote "$REMOTE_BUILD_JOBS") \
         DAEMON_IMAGE_NAMESPACE=$(quote_remote "$DAEMON_IMAGE_NAMESPACE") \
         DAEMON_IMAGE_TAG=$(quote_remote "$DAEMON_IMAGE_TAG") \
         RUN_USER=$(quote_remote "$RUN_USER") \
         RUN_GROUP=$(quote_remote "$RUN_GROUP") \
         bash -s" <<'REMOTE'
set -euo pipefail

SOURCE_ROOT="${INSTALL_ROOT}/source"
mkdir -p "${SOURCE_ROOT}" "${INSTALL_ROOT}/bin"

while IFS='|' read -r key ticker label daemon_name cli_name tx_name repo_url repo_branch image_repo; do
    [ -n "${key}" ] || continue
    image="${DAEMON_IMAGE_NAMESPACE}/${image_repo}:${DAEMON_IMAGE_TAG}"
    docker pull "${image}" >/dev/null
    cid="$(docker create "${image}")"
    docker cp "${cid}:/usr/local/bin/${daemon_name}" "${INSTALL_ROOT}/bin/${daemon_name}"
    docker cp "${cid}:/usr/local/bin/${cli_name}" "${INSTALL_ROOT}/bin/${cli_name}"
    docker cp "${cid}:/usr/local/bin/${tx_name}" "${INSTALL_ROOT}/bin/${tx_name}"
    docker rm -f "${cid}" >/dev/null
done <<'MAP'
blakecoin|BLC|Blakecoin|blakecoind|blakecoin-cli|blakecoin-tx|https://github.com/BlueDragon747/Blakecoin.git|master|blakecoin
blakebitcoin|BBTC|BlakeBitcoin|blakebitcoind|blakebitcoin-cli|blakebitcoin-tx|https://github.com/BlakeBitcoin/BlakeBitcoin.git|master|blakebitcoin
electron|ELT|Electron|electrond|electron-cli|electron-tx|https://github.com/BlueDragon747/Electron-ELT.git|master|electron
lithium|LIT|Lithium|lithiumd|lithium-cli|lithium-tx|https://github.com/BlueDragon747/lithium.git|master|lithium
photon|PHO|Photon|photond|photon-cli|photon-tx|https://github.com/BlueDragon747/photon.git|master|photon
universalmol|UMO|UniversalMolecule|universalmoleculed|universalmolecule-cli|universalmolecule-tx|https://github.com/BlueDragon747/universalmol.git|master|universalmolecule
MAP

solver_src="${SOURCE_ROOT}/solver-blakecoin"
rm -rf "${solver_src}"
git clone --depth 1 -b master https://github.com/BlueDragon747/Blakecoin.git "${solver_src}" >/dev/null 2>&1
g++ -O3 \
    -I "${solver_src}/src/crypto/blake" \
    -I "${solver_src}/src" \
    -o "${INSTALL_ROOT}/bin/solve_blake_header" \
    "${INSTALL_ROOT}/share/solve_blake_header.c" \
    "${solver_src}/src/crypto/blake/blake.cpp"

chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_ROOT}/bin" "${SOURCE_ROOT}"
REMOTE
fi

say "Creating daemon data directories, configs, and optional bootstrap files"
run_ssh \
    "DATA_ROOT=$(quote_remote "$DATA_ROOT") \
     RUN_USER=$(quote_remote "$RUN_USER") \
     RUN_GROUP=$(quote_remote "$RUN_GROUP") \
     NODE_RPC_USER=$(quote_remote "$NODE_RPC_USER") \
     NODE_RPC_PASS=$(quote_remote "$NODE_RPC_PASS") \
     NETWORK_MODE=$(quote_remote "$NETWORK_MODE") \
     USE_EXPLORER_PEERS=$(quote_remote "$USE_EXPLORER_PEERS") \
     DOWNLOAD_BOOTSTRAP=$(quote_remote "$DOWNLOAD_BOOTSTRAP") \
     BOOTSTRAP_BASE_URL=$(quote_remote "$BOOTSTRAP_BASE_URL") \
     RPC_PORT_BLC=$(quote_remote "$RPC_PORT_BLC") P2P_PORT_BLC=$(quote_remote "$P2P_PORT_BLC") P2P_PORT_BLC_PEER=$(quote_remote "$P2P_PORT_BLC_PEER") \
     RPC_PORT_BLC_PEER=$(quote_remote "$RPC_PORT_BLC_PEER") \
     RPC_PORT_BBTC=$(quote_remote "$RPC_PORT_BBTC") P2P_PORT_BBTC=$(quote_remote "$P2P_PORT_BBTC") P2P_PORT_BBTC_PEER=$(quote_remote "$P2P_PORT_BBTC_PEER") \
     RPC_PORT_BBTC_PEER=$(quote_remote "$RPC_PORT_BBTC_PEER") \
     RPC_PORT_ELT=$(quote_remote "$RPC_PORT_ELT") P2P_PORT_ELT=$(quote_remote "$P2P_PORT_ELT") P2P_PORT_ELT_PEER=$(quote_remote "$P2P_PORT_ELT_PEER") \
     RPC_PORT_ELT_PEER=$(quote_remote "$RPC_PORT_ELT_PEER") \
     RPC_PORT_LIT=$(quote_remote "$RPC_PORT_LIT") P2P_PORT_LIT=$(quote_remote "$P2P_PORT_LIT") P2P_PORT_LIT_PEER=$(quote_remote "$P2P_PORT_LIT_PEER") \
     RPC_PORT_LIT_PEER=$(quote_remote "$RPC_PORT_LIT_PEER") \
     RPC_PORT_PHO=$(quote_remote "$RPC_PORT_PHO") P2P_PORT_PHO=$(quote_remote "$P2P_PORT_PHO") P2P_PORT_PHO_PEER=$(quote_remote "$P2P_PORT_PHO_PEER") \
     RPC_PORT_PHO_PEER=$(quote_remote "$RPC_PORT_PHO_PEER") \
     RPC_PORT_UMO=$(quote_remote "$RPC_PORT_UMO") P2P_PORT_UMO=$(quote_remote "$P2P_PORT_UMO") P2P_PORT_UMO_PEER=$(quote_remote "$P2P_PORT_UMO_PEER") \
     RPC_PORT_UMO_PEER=$(quote_remote "$RPC_PORT_UMO_PEER") \
     bash -s" <<'REMOTE'
set -euo pipefail

fetch_chainz_nodes() {
    local symbol="$1"
    curl -fsSL "https://chainz.cryptoid.info/${symbol}/api.dws?q=nodes" 2>/dev/null \
        | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' \
        | grep -v '^0\.' \
        | sort -u \
        | sed 's/^/addnode=/'
}

write_config() {
    local coin_dir="$1"
    local data_suffix="$2"
    local config_name="$3"
    local rpc_port="$4"
    local p2p_port="$5"
    local local_peer_port="$6"
    local explorer_symbol="$7"
    local bootstrap_name="$8"
    local datadir="${DATA_ROOT}/${coin_dir}${data_suffix}"
    local conf_path="${datadir}/${config_name}"
    mkdir -p "${datadir}"
    {
        echo "# BlakeStream ${coin_dir}${data_suffix} generated config"
        echo "rpcuser=${NODE_RPC_USER}"
        echo "rpcpassword=${NODE_RPC_PASS}"
        echo "rpcport=${rpc_port}"
        echo "rpcbind=127.0.0.1"
        echo "rpcallowip=127.0.0.1"
        echo "port=${p2p_port}"
        echo "listen=1"
        echo "server=1"
        echo "daemon=0"
        echo "txindex=1"
        echo "fallbackfee=0.0001"
        echo "bind=127.0.0.1"
        echo "discover=0"
        echo "listenonion=0"
        echo "upnp=0"
        echo "maxtipage=3153600000"
        case "${NETWORK_MODE}" in
            testnet)
                echo "testnet=1"
                echo "dnsseed=0"
                echo "fixedseeds=0"
                ;;
            regtest)
                echo "regtest=1"
                echo "dnsseed=0"
                echo "fixedseeds=0"
                ;;
        esac
        if [ -n "${local_peer_port}" ]; then
            echo "addnode=127.0.0.1:${local_peer_port}"
        fi
        if [ "${USE_EXPLORER_PEERS}" = "true" ] && [ "${NETWORK_MODE}" = "mainnet" ]; then
            fetch_chainz_nodes "${explorer_symbol}" || true
        fi
    } > "${conf_path}"
    chmod 600 "${conf_path}"

    if [ "${DOWNLOAD_BOOTSTRAP}" = "true" ] && [ "${NETWORK_MODE}" = "mainnet" ] && [ -z "${data_suffix}" ]; then
        bootstrap_file="${datadir}/bootstrap.dat"
        if [ ! -s "${bootstrap_file}" ]; then
            bootstrap_url="${BOOTSTRAP_BASE_URL%/}/${bootstrap_name}/bootstrap.dat"
            if ! wget -q -O "${bootstrap_file}" "${bootstrap_url}"; then
                rm -f "${bootstrap_file}"
            fi
        fi
    fi
}

if [ "${DOWNLOAD_BOOTSTRAP}" = "true" ] && [ "${NETWORK_MODE}" != "mainnet" ]; then
    echo "bootstrap download requested but skipped for ${NETWORK_MODE}" >&2
fi

write_config "blakecoin" "" "blakecoin.conf" "${RPC_PORT_BLC}" "${P2P_PORT_BLC}" "${P2P_PORT_BLC_PEER}" "blc" "Blakecoin"
write_config "blakecoin" "-peer" "blakecoin.conf" "${RPC_PORT_BLC_PEER}" "${P2P_PORT_BLC_PEER}" "${P2P_PORT_BLC}" "blc" "Blakecoin"
write_config "blakebitcoin" "" "blakebitcoin.conf" "${RPC_PORT_BBTC}" "${P2P_PORT_BBTC}" "${P2P_PORT_BBTC_PEER}" "bbtc" "BlakeBitcoin"
write_config "blakebitcoin" "-peer" "blakebitcoin.conf" "${RPC_PORT_BBTC_PEER}" "${P2P_PORT_BBTC_PEER}" "${P2P_PORT_BBTC}" "bbtc" "BlakeBitcoin"
write_config "electron" "" "electron.conf" "${RPC_PORT_ELT}" "${P2P_PORT_ELT}" "${P2P_PORT_ELT_PEER}" "elt" "Electron"
write_config "electron" "-peer" "electron.conf" "${RPC_PORT_ELT_PEER}" "${P2P_PORT_ELT_PEER}" "${P2P_PORT_ELT}" "elt" "Electron"
write_config "lithium" "" "lithium.conf" "${RPC_PORT_LIT}" "${P2P_PORT_LIT}" "${P2P_PORT_LIT_PEER}" "lit" "Lithium"
write_config "lithium" "-peer" "lithium.conf" "${RPC_PORT_LIT_PEER}" "${P2P_PORT_LIT_PEER}" "${P2P_PORT_LIT}" "lit" "Lithium"
write_config "photon" "" "photon.conf" "${RPC_PORT_PHO}" "${P2P_PORT_PHO}" "${P2P_PORT_PHO_PEER}" "pho" "Photon"
write_config "photon" "-peer" "photon.conf" "${RPC_PORT_PHO_PEER}" "${P2P_PORT_PHO_PEER}" "${P2P_PORT_PHO}" "pho" "Photon"
write_config "universalmol" "" "universalmolecule.conf" "${RPC_PORT_UMO}" "${P2P_PORT_UMO}" "${P2P_PORT_UMO_PEER}" "umo" "UniversalMolecule"
write_config "universalmol" "-peer" "universalmolecule.conf" "${RPC_PORT_UMO_PEER}" "${P2P_PORT_UMO_PEER}" "${P2P_PORT_UMO}" "umo" "UniversalMolecule"

chown -R "${RUN_USER}:${RUN_GROUP}" "${DATA_ROOT}"
REMOTE

say "Creating a dedicated Python venv for the pool, proxy, and dashboard"
run_ssh \
    "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RUN_USER=$(quote_remote "$RUN_USER") RUN_GROUP=$(quote_remote "$RUN_GROUP") bash -s" <<'REMOTE'
set -euo pipefail
python3 -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/pip" install --upgrade pip setuptools wheel >/dev/null
"${INSTALL_ROOT}/venv/bin/pip" install Flask Twisted base58 pyasynchat pyasyncore "git+https://github.com/SidGrip/python-bitcoinrpc.git" >/dev/null
chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_ROOT}/venv"
REMOTE

say "Installing daemon service units"
run_scp "$STAGE_ROOT/systemd/"*.service "${USER}@${HOST}:/etc/systemd/system/"
run_ssh "systemctl daemon-reload"

say "Starting all 12 ${NETWORK_DISPLAY} daemons (primary + local peer per chain)"
run_ssh <<REMOTE
set -euo pipefail
systemctl enable --now \
  blakestream-testnet-blakecoin.service \
  blakestream-testnet-blakecoin-peer.service \
  blakestream-testnet-blakebitcoin.service \
  blakestream-testnet-blakebitcoin-peer.service \
  blakestream-testnet-electron.service \
  blakestream-testnet-electron-peer.service \
  blakestream-testnet-lithium.service \
  blakestream-testnet-lithium-peer.service \
  blakestream-testnet-photon.service \
  blakestream-testnet-photon-peer.service \
  blakestream-testnet-universalmol.service \
  blakestream-testnet-universalmol-peer.service
REMOTE

wait_for_rpc() {
    local port="$1"
    local label="$2"
    say "Waiting for ${label} RPC on ${port}"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") PORT=$(quote_remote "$port") bash -s" <<'REMOTE'
set -euo pipefail
for _ in $(seq 1 120); do
    if "${INSTALL_ROOT}/bin/rpc_call.py" "${PORT}" getblockchaininfo >/dev/null 2>&1; then
        exit 0
    fi
    sleep 1
done
exit 1
REMOTE
}

wait_for_rpc "$RPC_PORT_BLC" "Blakecoin"
wait_for_rpc "$RPC_PORT_BBTC" "BlakeBitcoin"
wait_for_rpc "$RPC_PORT_ELT" "Electron"
wait_for_rpc "$RPC_PORT_LIT" "Lithium"
wait_for_rpc "$RPC_PORT_PHO" "Photon"
wait_for_rpc "$RPC_PORT_UMO" "UniversalMolecule"

wait_for_peer() {
    local port="$1"
    local label="$2"
    say "Waiting for ${label} to establish a local peer"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") PORT=$(quote_remote "$port") bash -s" <<'REMOTE'
set -euo pipefail
for _ in $(seq 1 120); do
    connections="$("${INSTALL_ROOT}/bin/rpc_call.py" "${PORT}" getconnectioncount 2>/dev/null || echo 0)"
    if [ "${connections:-0}" -gt 0 ]; then
        exit 0
    fi
    sleep 1
done
exit 1
REMOTE
}

wait_for_peer "$RPC_PORT_BLC" "Blakecoin"
wait_for_peer "$RPC_PORT_BBTC" "BlakeBitcoin"
wait_for_peer "$RPC_PORT_ELT" "Electron"
wait_for_peer "$RPC_PORT_LIT" "Lithium"
wait_for_peer "$RPC_PORT_PHO" "Photon"
wait_for_peer "$RPC_PORT_UMO" "UniversalMolecule"

say "Importing the V2 test miner private key into all 6 wallets"
for port in "$RPC_PORT_BLC" "$RPC_PORT_BBTC" "$RPC_PORT_ELT" "$RPC_PORT_LIT" "$RPC_PORT_PHO" "$RPC_PORT_UMO"; do
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") PORT=$(quote_remote "$port") KEY=$(quote_remote "$MINER_PRIVATE_KEY") bash -s" <<'REMOTE'
set -euo pipefail
"${INSTALL_ROOT}/bin/rpc_call.py" "${PORT}" importprivkey "${KEY}" test-miner false >/dev/null 2>&1 || true
REMOTE
done

say "Generating TrackerAddr on Blakecoin ${NETWORK_DISPLAY}"
TRACKER_ADDR="$(
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") RPC_PORT_BLC=$(quote_remote "$RPC_PORT_BLC") bash -s" <<'REMOTE'
set -euo pipefail
"${INSTALL_ROOT}/bin/rpc_call.py" "${RPC_PORT_BLC}" getnewaddress pool-tracker legacy
REMOTE
)"

derive_v2_address() {
    local hrp="$1"
    PYTHONPATH="$ROOT" python3 - <<PY
from mining_key import address_from_v2_mining_key
print(address_from_v2_mining_key("${MINER_USERNAME}", "${hrp}"))
PY
}

if [ -z "$MINER_PAYOUT_ADDRESS" ]; then
    MINER_PAYOUT_ADDRESS="$(derive_v2_address "${BLC_HRP}")"
fi

say "Generating pool-controlled aux payout addresses on each child chain"
generate_pool_aux_address() {
    local port="$1"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") PORT=$(quote_remote "$port") ADDR_TYPE=$(quote_remote "$POOL_AUX_ADDRESS_TYPE") bash -s" <<'REMOTE'
set -euo pipefail
"${INSTALL_ROOT}/bin/rpc_call.py" "${PORT}" getnewaddress pool-aux "${ADDR_TYPE}"
REMOTE
}

POOL_AUX_ADDRESS_BBTC="$(generate_pool_aux_address "$RPC_PORT_BBTC")"
POOL_AUX_ADDRESS_ELT="$(generate_pool_aux_address "$RPC_PORT_ELT")"
POOL_AUX_ADDRESS_LIT="$(generate_pool_aux_address "$RPC_PORT_LIT")"
POOL_AUX_ADDRESS_PHO="$(generate_pool_aux_address "$RPC_PORT_PHO")"
POOL_AUX_ADDRESS_UMO="$(generate_pool_aux_address "$RPC_PORT_UMO")"

say "Verifying the bare V2 mining key resolves to the expected Blakecoin ${NETWORK_DISPLAY} bech32 address"
DERIVED_CHECK="$(
    PYTHONPATH="$ROOT" python3 - <<PY
from mining_key import address_from_v2_mining_key
print(address_from_v2_mining_key("${MINER_USERNAME}", "${BLC_HRP}"))
PY
)"
if [ "$DERIVED_CHECK" != "$MINER_PAYOUT_ADDRESS" ]; then
    die "derived V2 address mismatch: expected ${MINER_PAYOUT_ADDRESS}, got ${DERIVED_CHECK}"
fi

say "Rendering pool/proxy/dashboard/miner config"
cat > "$STAGE_ROOT/config/config.py" <<EOF
ServerName = 'BlakeStream AuxPoW Testnet VPS'
NoInteractive = True
SkipBdiff1Floor = True
ShareTarget = 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff
DynamicTargetting = 0
DynamicTargetGoal = 8
DynamicTargetWindow = 120
DynamicTargetQuick = True
AllowShareDifficultyAboveNetwork = True
WorkQueueSizeRegular = (0x10, 0x100)
WorkQueueSizeClear = (0x100, 0x200)
WorkQueueSizeLongpoll = (0x100, 0x200)
WorkUpdateInterval = 5
MinimumTxnUpdateWait = 1
TxnUpdateRetryWait = 1
IdleSleepTime = 0.1
TrackerAddr = '${TRACKER_ADDR}'
CoinbaserCmd = '${INSTALL_ROOT}/bin/coinbaser.py %d %p'
TemplateSources = (
    {
        'name': 'blakecoin-${NETWORK_DISPLAY}',
        'uri': 'http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_BLC}/',
        'priority': 0,
        'weight': 1,
    },
)
TemplateChecks = None
BlockSubmissions = (
    {
        'name': 'blakecoin-${NETWORK_DISPLAY}',
        'uri': 'http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_BLC}/',
    },
)
MinimumTemplateAcceptanceRatio = 0
MinimumTemplateScore = 1
DelayLogForUpstream = False
UpstreamBitcoindNode = ('127.0.0.1', ${P2P_PORT_BLC})
UpstreamNetworkId = b'\\xfc\\xc1\\xb7\\xdc'
SecretUser = '${POOL_SECRET_USER}'
GotWorkURI = 'http://${POOL_SECRET_USER}:${POOL_SECRET_PASS}@127.0.0.1:${PROXY_PORT}/'
GotWorkTarget = 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
POT = 2
Greedy = False
JSONRPCAddresses = (('::ffff:127.0.0.1', ${POOL_JSONRPC_PORT}),)
StratumAddresses = (('0.0.0.0', ${STRATUM_PORT}),)
BitcoinNodeAddresses = ()
TrustedForwarders = ('::ffff:127.0.0.1', '127.0.0.1')
ShareLogging = (
    {
        'type': 'logfile',
        'filename': '${LOG_ROOT}/share-logfile',
        'format': "{time} {Q(remoteHost)} {username} {YN(not(rejectReason))} {dash(YN(upstreamResult))} {dash(rejectReason)} {dash(target2bdiff(target))} {dash(_targethex)} {solution}\\n",
    },
)
Authentication = ({'module': 'allowall'},)
EOF

cat > "$STAGE_ROOT/config/pool.env" <<EOF
PYTHONPATH=${INSTALL_ROOT}/config:${INSTALL_ROOT}/eloipool:${INSTALL_ROOT}/eloipool/vendor:${INSTALL_ROOT}
PYTHONUNBUFFERED=1
COINBASER_SHARE_LOG=${LOG_ROOT}/share-logfile
COINBASER_WINDOW=500
COINBASER_POOL_KEEP_BPS=100
COINBASER_MINING_KEY_SEGWIT_HRP=tblc
COINBASER_DEBUG_LOG=${LOG_ROOT}/coinbaser.jsonl
COINBASER_ELOIPOOL_DIR=${INSTALL_ROOT}/eloipool
EOF

cat > "$STAGE_ROOT/dashboard/dashboard.env" <<EOF
DASH_BIND=0.0.0.0:${DASHBOARD_PORT}
DASH_RPC_URL=http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_BLC}/
DASH_CHILD_RPC_URLS={"BlakeBitcoin":"http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_BBTC}/","Electron":"http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_ELT}/","Lithium":"http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_LIT}/","Photon":"http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_PHO}/","UniversalMolecule":"http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_UMO}/"}
DASH_CHAIN_TICKERS={"Blakecoin":"BLC","BlakeBitcoin":"BBTC","Electron":"ELT","Lithium":"LIT","Photon":"PHO","UniversalMolecule":"UMO"}
DASH_POOL_LOG=${LOG_ROOT}/eloipool.stderr
DASH_PROXY_LOG=${LOG_ROOT}/merged-mine-proxy.log
DASH_SHARE_LOG=${LOG_ROOT}/share-logfile
DASH_TRACKER_ADDR=${TRACKER_ADDR}
DASH_STRATUM_HOST=${PUBLIC_HOST}
DASH_STRATUM_PORT=${STRATUM_PORT}
DASH_HEADER_TITLE=${DASH_HEADER_TITLE}
DASH_HEADER_SUBTITLE=${DASH_HEADER_SUBTITLE}
DASH_MINING_KEY_SEGWIT_HRP=${BLC_HRP}
DASH_MINING_KEY_V2_COIN_HRPS={"BlakeBitcoin":"${BBTC_HRP}","Electron":"${ELT_HRP}","Lithium":"${LIT_HRP}","Photon":"${PHO_HRP}","UniversalMolecule":"${UMO_HRP}"}
DASH_COINBASER_DEBUG_LOG=${LOG_ROOT}/coinbaser.jsonl
DASH_AUX_POOL_ADDRESSES={"BlakeBitcoin":"${POOL_AUX_ADDRESS_BBTC}","Electron":"${POOL_AUX_ADDRESS_ELT}","Lithium":"${POOL_AUX_ADDRESS_LIT}","Photon":"${POOL_AUX_ADDRESS_PHO}","UniversalMolecule":"${POOL_AUX_ADDRESS_UMO}"}
DASH_AUX_PAYOUT_MODE=pool
DASH_ELOIPOOL_PATH=${INSTALL_ROOT}/eloipool
DASH_COINBASER=${INSTALL_ROOT}/bin/coinbaser.py
DASH_MINER_LOG=${LOG_ROOT}/miner.log
EOF

cat > "$STAGE_ROOT/systemd/blakestream-testnet-pool.service" <<EOF
[Unit]
Description=BlakeStream full merged-mining ${NETWORK_DISPLAY} pool (Eloipool)
After=network-online.target blakestream-testnet-blakecoin.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_ROOT}/eloipool
EnvironmentFile=${INSTALL_ROOT}/config/pool.env
ExecStart=${INSTALL_ROOT}/venv/bin/python -u ${INSTALL_ROOT}/eloipool/eloipool.py
StandardOutput=append:${LOG_ROOT}/eloipool.stdout
StandardError=append:${LOG_ROOT}/eloipool.stderr
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > "$STAGE_ROOT/systemd/blakestream-testnet-proxy.service" <<EOF
[Unit]
Description=BlakeStream merged-mine proxy (${NETWORK_DISPLAY})
After=blakestream-testnet-pool.service blakestream-testnet-blakebitcoin.service blakestream-testnet-electron.service blakestream-testnet-lithium.service blakestream-testnet-photon.service blakestream-testnet-universalmol.service
Requires=blakestream-testnet-pool.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_ROOT}/eloipool
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_ROOT}/venv/bin/python -u ${INSTALL_ROOT}/eloipool/merged-mine-proxy.py3 -w ${PROXY_PORT} -p http://${POOL_SECRET_USER}:${POOL_SECRET_PASS}@127.0.0.1:${POOL_JSONRPC_PORT}/ -s ${MERKLE_SIZE} -x http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_BBTC}/ -a ${POOL_AUX_ADDRESS_BBTC} -x http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_ELT}/ -a ${POOL_AUX_ADDRESS_ELT} -x http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_LIT}/ -a ${POOL_AUX_ADDRESS_LIT} -x http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_PHO}/ -a ${POOL_AUX_ADDRESS_PHO} -x http://${NODE_RPC_USER}:${NODE_RPC_PASS}@127.0.0.1:${RPC_PORT_UMO}/ -a ${POOL_AUX_ADDRESS_UMO}
StandardOutput=append:${LOG_ROOT}/merged-mine-proxy.stdout
StandardError=append:${LOG_ROOT}/merged-mine-proxy.log
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > "$STAGE_ROOT/systemd/blakestream-testnet-dashboard.service" <<EOF
[Unit]
Description=BlakeStream ${NETWORK_DISPLAY} pool dashboard
After=blakestream-testnet-pool.service blakestream-testnet-proxy.service
Requires=blakestream-testnet-pool.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_ROOT}/dashboard
EnvironmentFile=${INSTALL_ROOT}/dashboard/dashboard.env
ExecStart=${INSTALL_ROOT}/venv/bin/python -u ${INSTALL_ROOT}/dashboard/dashboard.py
StandardOutput=append:${LOG_ROOT}/dashboard.stdout
StandardError=append:${LOG_ROOT}/dashboard.stderr
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > "$STAGE_ROOT/systemd/blakestream-testnet-miner.service" <<EOF
[Unit]
Description=BlakeStream single-core CPU miner (${NETWORK_DISPLAY})
After=blakestream-testnet-proxy.service
Requires=blakestream-testnet-proxy.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_ROOT}
Environment=PYTHONPATH=${INSTALL_ROOT}:${INSTALL_ROOT}/eloipool
Environment=PYTHONUNBUFFERED=1
Environment=STRATUM_HOST=127.0.0.1
Environment=STRATUM_PORT=${STRATUM_PORT}
Environment=STRATUM_USER=${MINER_USERNAME}
Environment=STRATUM_SHARE_COUNT=1000000
Environment=STRATUM_TARGET_MODE=network
Environment=STRATUM_SOLVER=${INSTALL_ROOT}/bin/solve_blake_header
ExecStart=${INSTALL_ROOT}/venv/bin/python -u ${INSTALL_ROOT}/bin/cpu_miner.py
StandardOutput=append:${LOG_ROOT}/miner.log
StandardError=append:${LOG_ROOT}/miner.log
Restart=always
RestartSec=5
CPUAffinity=0
Nice=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

say "Uploading pool/proxy/dashboard/miner config"
run_rsync "$STAGE_ROOT/config/" "${USER}@${HOST}:${INSTALL_ROOT}/config/"
run_rsync "$STAGE_ROOT/dashboard/" "${USER}@${HOST}:${INSTALL_ROOT}/dashboard/"
run_scp \
    "$STAGE_ROOT/systemd/blakestream-testnet-pool.service" \
    "$STAGE_ROOT/systemd/blakestream-testnet-proxy.service" \
    "$STAGE_ROOT/systemd/blakestream-testnet-dashboard.service" \
    "$STAGE_ROOT/systemd/blakestream-testnet-miner.service" \
    "${USER}@${HOST}:/etc/systemd/system/"
run_ssh "chown -R ${RUN_USER}:${RUN_GROUP} ${INSTALL_ROOT}/config ${INSTALL_ROOT}/dashboard"

say "Opening the public ${NETWORK_DISPLAY} pool ports when ufw is active"
run_ssh \
    "STRATUM_PORT=$(quote_remote "$STRATUM_PORT") DASHBOARD_PORT=$(quote_remote "$DASHBOARD_PORT") bash -s" <<'REMOTE'
set -euo pipefail
if command -v ufw >/dev/null 2>&1; then
    status="$(ufw status 2>/dev/null | head -n1 || true)"
    if printf '%s' "$status" | grep -qi active; then
        ufw allow "${STRATUM_PORT}/tcp" >/dev/null 2>&1 || true
        ufw allow "${DASHBOARD_PORT}/tcp" >/dev/null 2>&1 || true
    fi
fi
REMOTE

if [ "${ENABLE_CPU_MINER}" = "true" ]; then
    say "Starting pool, proxy, dashboard, and single-core miner"
    run_ssh <<REMOTE
set -euo pipefail
systemctl daemon-reload
systemctl enable --now \
  blakestream-testnet-pool.service \
  blakestream-testnet-proxy.service \
  blakestream-testnet-dashboard.service \
  blakestream-testnet-miner.service
REMOTE
else
    say "Starting pool, proxy, and dashboard"
    run_ssh <<REMOTE
set -euo pipefail
systemctl daemon-reload
systemctl enable --now \
  blakestream-testnet-pool.service \
  blakestream-testnet-proxy.service \
  blakestream-testnet-dashboard.service
REMOTE
fi

wait_for_service() {
    local svc="$1"
    say "Waiting for service ${svc}"
    run_ssh "for _ in \$(seq 1 60); do systemctl is-active --quiet $(quote_remote "$svc") && exit 0; sleep 1; done; exit 1"
}

wait_for_service "blakestream-testnet-pool.service"
wait_for_service "blakestream-testnet-proxy.service"
wait_for_service "blakestream-testnet-dashboard.service"
if [ "${ENABLE_CPU_MINER}" = "true" ]; then
    wait_for_service "blakestream-testnet-miner.service"

    say "Waiting for parent and child heights to start moving under live mining"
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") RPC_PORT_BLC=$(quote_remote "$RPC_PORT_BLC") RPC_PORT_BBTC=$(quote_remote "$RPC_PORT_BBTC") RPC_PORT_ELT=$(quote_remote "$RPC_PORT_ELT") RPC_PORT_LIT=$(quote_remote "$RPC_PORT_LIT") RPC_PORT_PHO=$(quote_remote "$RPC_PORT_PHO") RPC_PORT_UMO=$(quote_remote "$RPC_PORT_UMO") bash -s" <<'REMOTE'
set -euo pipefail
get_height() {
    "${INSTALL_ROOT}/bin/rpc_call.py" "$1" getblockcount
}

blc0="$(get_height "${RPC_PORT_BLC}")"
bbtc0="$(get_height "${RPC_PORT_BBTC}")"
elt0="$(get_height "${RPC_PORT_ELT}")"
lit0="$(get_height "${RPC_PORT_LIT}")"
pho0="$(get_height "${RPC_PORT_PHO}")"
umo0="$(get_height "${RPC_PORT_UMO}")"

for _ in $(seq 1 180); do
    blc="$(get_height "${RPC_PORT_BLC}" || echo "$blc0")"
    bbtc="$(get_height "${RPC_PORT_BBTC}" || echo "$bbtc0")"
    elt="$(get_height "${RPC_PORT_ELT}" || echo "$elt0")"
    lit="$(get_height "${RPC_PORT_LIT}" || echo "$lit0")"
    pho="$(get_height "${RPC_PORT_PHO}" || echo "$pho0")"
    umo="$(get_height "${RPC_PORT_UMO}" || echo "$umo0")"
    if [ "$blc" -gt "$blc0" ] && [ "$bbtc" -gt "$bbtc0" ] && [ "$elt" -gt "$elt0" ] && [ "$lit" -gt "$lit0" ] && [ "$pho" -gt "$pho0" ] && [ "$umo" -gt "$umo0" ]; then
        exit 0
    fi
    sleep 2
done
exit 1
REMOTE
fi

say "Collecting live status summary"
REMOTE_SUMMARY="$(
    run_ssh \
        "INSTALL_ROOT=$(quote_remote "$INSTALL_ROOT") RPC_USER=$(quote_remote "$NODE_RPC_USER") RPC_PASSWORD=$(quote_remote "$NODE_RPC_PASS") LOG_ROOT=$(quote_remote "$LOG_ROOT") RPC_PORT_BLC=$(quote_remote "$RPC_PORT_BLC") RPC_PORT_BBTC=$(quote_remote "$RPC_PORT_BBTC") RPC_PORT_ELT=$(quote_remote "$RPC_PORT_ELT") RPC_PORT_LIT=$(quote_remote "$RPC_PORT_LIT") RPC_PORT_PHO=$(quote_remote "$RPC_PORT_PHO") RPC_PORT_UMO=$(quote_remote "$RPC_PORT_UMO") ENABLE_CPU_MINER=$(quote_remote "$ENABLE_CPU_MINER") NETWORK_MODE=$(quote_remote "$NETWORK_MODE") bash -s" <<'REMOTE'
set -euo pipefail
rpc() { "${INSTALL_ROOT}/bin/rpc_call.py" "$1" "$2"; }
printf 'Blakecoin height: %s\n' "$(rpc "${RPC_PORT_BLC}" getblockcount)"
printf 'BlakeBitcoin height: %s\n' "$(rpc "${RPC_PORT_BBTC}" getblockcount)"
printf 'Electron height: %s\n' "$(rpc "${RPC_PORT_ELT}" getblockcount)"
printf 'Lithium height: %s\n' "$(rpc "${RPC_PORT_LIT}" getblockcount)"
printf 'Photon height: %s\n' "$(rpc "${RPC_PORT_PHO}" getblockcount)"
printf 'UniversalMolecule height: %s\n' "$(rpc "${RPC_PORT_UMO}" getblockcount)"
printf 'Pool services:\n'
if [ "${ENABLE_CPU_MINER}" = "true" ]; then
    systemctl --no-pager --plain --full status \
      blakestream-testnet-pool.service \
      blakestream-testnet-proxy.service \
      blakestream-testnet-dashboard.service \
      blakestream-testnet-miner.service \
      | sed -n '1,40p'
else
    systemctl --no-pager --plain --full status \
      blakestream-testnet-pool.service \
      blakestream-testnet-proxy.service \
      blakestream-testnet-dashboard.service \
      | sed -n '1,40p'
fi
printf '\nRecent miner log:\n'
if [ "${ENABLE_CPU_MINER}" = "true" ]; then
    tail -n 20 "${LOG_ROOT}/miner.log" || true
else
    printf 'skipped on %s deploys\n' "${NETWORK_MODE}"
fi
REMOTE
)"

printf '\n%s\n' "$REMOTE_SUMMARY"
printf '\nDashboard: http://%s:%s/\n' "$PUBLIC_HOST" "$DASHBOARD_PORT"
printf 'Stratum : stratum+tcp://%s:%s\n' "$PUBLIC_HOST" "$STRATUM_PORT"
if [ "${ENABLE_CPU_MINER}" = "true" ]; then
    printf 'Miner   : bare V2 mining key %s\n' "$MINER_USERNAME"
else
    printf 'Miner   : skipped on %s deploys\n' "$NETWORK_DISPLAY"
fi
printf 'Payout  : %s\n' "$MINER_PAYOUT_ADDRESS"
