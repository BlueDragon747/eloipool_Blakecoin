#!/usr/bin/env python3
"""Persistent private 25.2 testnet pool controller for DEX QA.

This script deliberately uses a RAM runtime root and leaves existing daemon
data directories alone. It starts a local-only private testnet stack:

  BLC parent daemon + BBTC/ELT/LIT/PHO/UMO AuxPoW child daemons
  Go Eloipool stratum/proxy/dashboard
  bundled CPU miner loop

It is for feature readiness testing only. It never touches mainnet.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("DEX25_TESTNET_POOL_ROOT", "/mnt/ram-build/dex25-current-testnet-pool"))
STAGE_ROOT = Path(os.environ.get("DEX25_STAGE_ROOT", "/mnt/ram-build/dex25-hub-qa-20260526"))
POOL_BIN = Path(os.environ.get("DEX25_ELOIPOOL_BIN", str(STAGE_ROOT / "eliopool" / "bin" / "eloipool")))
CPU_MINER = Path(os.environ.get("DEX25_CPU_MINER", str(STAGE_ROOT / "eliopool" / "deploy-bundle" / "cpu_miner.py")))
RPC_USER = os.environ.get("DEX25_TESTNET_RPC_USER", "dex-testnet")
RPC_PASSWORD = os.environ.get("DEX25_TESTNET_RPC_PASSWORD", "dex-testnet-pass")


@dataclass(frozen=True)
class Coin:
    ticker: str
    coin_name: str
    daemon_name: str
    cli_name: str
    rpc_port: int
    p2p_port: int
    peer_rpc_port: int
    peer_p2p_port: int
    aux_name: str | None = None

    @property
    def data_dir(self) -> Path:
        return ROOT / "daemon-data" / self.ticker

    @property
    def binary(self) -> Path:
        return ROOT / "bin" / self.ticker / self.daemon_name

    @property
    def peer_data_dir(self) -> Path:
        return ROOT / "daemon-peer-data" / self.ticker

    @property
    def log_file(self) -> Path:
        return ROOT / "logs" / f"{self.ticker}-daemon.log"

    @property
    def peer_log_file(self) -> Path:
        return ROOT / "peer-logs" / f"{self.ticker}-peer.log"

    @property
    def pid_file(self) -> Path:
        return ROOT / "run" / f"{self.ticker}.pid"

    @property
    def peer_pid_file(self) -> Path:
        return ROOT / "run" / f"{self.ticker}-peer.pid"


COINS: list[Coin] = [
    Coin("BLC", "Blakecoin", "blakecoind", "blakecoin-cli", 59320, 60320, 59420, 60420),
    Coin("BBTC", "BlakeBitcoin", "blakebitcoind", "blakebitcoin-cli", 59322, 60322, 59422, 60422, "BlakeBitcoin"),
    Coin("ELT", "Electron", "electrond", "electron-cli", 59324, 60324, 59424, 60424, "Electron"),
    Coin("LIT", "Lithium", "lithiumd", "lithium-cli", 59326, 60326, 59426, 60426, "Lithium"),
    Coin("PHO", "Photon", "photond", "photon-cli", 59328, 60328, 59428, 60428, "Photon"),
    Coin("UMO", "UniversalMolecule", "universalmoleculed", "universalmolecule-cli", 59330, 60330, 59430, 60430, "UniversalMolecule"),
]


def coin_by_ticker(ticker: str) -> Coin:
    for coin in COINS:
        if coin.ticker == ticker.upper():
            return coin
    raise SystemExit(f"unknown ticker: {ticker}")


def ensure_dirs() -> None:
    for path in (ROOT / "bin", ROOT / "daemon-data", ROOT / "daemon-peer-data", ROOT / "logs", ROOT / "peer-logs", ROOT / "run", ROOT / "evidence"):
        path.mkdir(parents=True, exist_ok=True)


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw.isdigit():
        return int(raw)
    return None


def pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def rpc_url(coin: Coin, wallet: str | None = None, *, peer: bool = False) -> str:
    suffix = f"/wallet/{wallet}" if wallet else ""
    port = coin.peer_rpc_port if peer else coin.rpc_port
    return f"http://127.0.0.1:{port}{suffix}"


def rpc_call(coin: Coin, method: str, params: list[Any] | None = None, wallet: str | None = None, timeout: float = 10.0, *, peer: bool = False) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": "dex25-testnet", "method": method, "params": params or []}).encode("utf-8")
    token = base64.b64encode(f"{RPC_USER}:{RPC_PASSWORD}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        rpc_url(coin, wallet, peer=peer),
        data=body,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"{coin.ticker} RPC {method} HTTP {exc.code}") from exc
    if payload.get("error"):
        raise RuntimeError(f"{coin.ticker} RPC {method}: {payload['error']}")
    return payload.get("result")


def wait_rpc(coin: Coin, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            rpc_call(coin, "getblockchaininfo", timeout=2)
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"{coin.ticker} RPC did not become ready: {last_error}")


def source_binary(coin: Coin) -> Path:
    repo_map = {
        "BLC": Path("/home/sid/Blakestream-Installer/repos/Blakecoin-0.25.2/src/blakecoind"),
        "BBTC": Path("/home/sid/Blakestream-Installer/repos/BlakeBitcoin-0.25.2/src/blakebitcoind"),
        "ELT": Path("/home/sid/Blakestream-Installer/repos/Electron-ELT-0.25.2/src/electrond"),
        "LIT": Path("/home/sid/Blakestream-Installer/repos/lithium-0.25.2/src/lithiumd"),
        "PHO": Path("/home/sid/Blakestream-Installer/repos/Photon-0.25.2/src/photond"),
        "UMO": Path("/home/sid/Blakestream-Installer/repos/universalmolecule-0.25.2/src/universalmoleculed"),
    }
    candidate = repo_map.get(coin.ticker)
    if candidate and candidate.exists():
        return candidate
    staged = STAGE_ROOT / "daemons" / coin.ticker / coin.daemon_name
    if staged.exists():
        return staged
    raise FileNotFoundError(f"missing daemon binary for {coin.ticker}")


def install_binaries(force: bool = False) -> dict[str, str]:
    ensure_dirs()
    installed: dict[str, str] = {}
    for coin in COINS:
        src = source_binary(coin)
        dst = coin.binary
        dst.parent.mkdir(parents=True, exist_ok=True)
        if force or not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            subprocess.run(["cp", "-f", str(src), str(dst)], check=True)
            dst.chmod(0o755)
        installed[coin.ticker] = str(dst)
    return installed


def start_daemon(coin: Coin) -> None:
    if pid_running(read_pid(coin.pid_file)) or port_open("127.0.0.1", coin.rpc_port):
        return
    coin.data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(coin.binary),
        "-testnet=1",
        f"-datadir={coin.data_dir}",
        "-server=1",
        "-listen=1",
        "-dnsseed=0",
        "-fixedseeds=0",
        "-discover=0",
        "-listenonion=0",
        "-maxtipage=3153600000",
        "-txindex=1",
        "-fallbackfee=0.0001",
        "-bind=127.0.0.1",
        "-rpcbind=127.0.0.1",
        "-rpcallowip=127.0.0.1",
        f"-rpcuser={RPC_USER}",
        f"-rpcpassword={RPC_PASSWORD}",
        f"-rpcport={coin.rpc_port}",
        f"-port={coin.p2p_port}",
    ]
    with coin.log_file.open("ab") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    coin.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")


def start_peer_daemon(coin: Coin) -> None:
    if pid_running(read_pid(coin.peer_pid_file)) or port_open("127.0.0.1", coin.peer_rpc_port):
        return
    coin.peer_data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(coin.binary),
        "-testnet=1",
        f"-datadir={coin.peer_data_dir}",
        "-server=1",
        "-listen=1",
        "-dnsseed=0",
        "-fixedseeds=0",
        "-discover=0",
        "-listenonion=0",
        "-maxtipage=3153600000",
        "-txindex=1",
        "-fallbackfee=0.0001",
        "-bind=127.0.0.1",
        "-rpcbind=127.0.0.1",
        "-rpcallowip=127.0.0.1",
        f"-rpcuser={RPC_USER}",
        f"-rpcpassword={RPC_PASSWORD}",
        f"-rpcport={coin.peer_rpc_port}",
        f"-port={coin.peer_p2p_port}",
        f"-connect=127.0.0.1:{coin.p2p_port}",
    ]
    with coin.peer_log_file.open("ab") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    coin.peer_pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")


def wait_peer_ports(coin: Coin, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open("127.0.0.1", coin.peer_rpc_port) and port_open("127.0.0.1", coin.peer_p2p_port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{coin.ticker} peer daemon did not open RPC/P2P ports")


def connect_peer(coin: Coin, timeout: float = 30.0) -> None:
    try:
        rpc_call(coin, "addnode", [f"127.0.0.1:{coin.peer_p2p_port}", "onetry"], timeout=3)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = rpc_call(coin, "getnetworkinfo", timeout=3)
            if int(info.get("connections", 0)) > 0:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{coin.ticker} did not connect to its local peer")


def stop_pid(path: Path, grace: float = 10.0) -> bool:
    pid = read_pid(path)
    if not pid_running(pid):
        try:
            path.unlink()
        except OSError:
            pass
        return True
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not pid_running(pid):
            try:
                path.unlink()
            except OSError:
                pass
            return True
        time.sleep(0.2)
    return False


def ensure_pool_wallet(coin: Coin) -> str:
    wallets = rpc_call(coin, "listwallets")
    if "pool" not in wallets:
        try:
            rpc_call(coin, "loadwallet", ["pool"])
        except Exception:
            try:
                rpc_call(coin, "createwallet", ["pool"])
            except Exception:
                rpc_call(coin, "createwallet", ["pool", False, False, "", False, False])
    try:
        return str(rpc_call(coin, "getnewaddress", ["", "bech32"], wallet="pool"))
    except Exception:
        return str(rpc_call(coin, "getnewaddress", [], wallet="pool"))


def pool_command(addresses: dict[str, str]) -> list[str]:
    parent = coin_by_ticker("BLC")
    command = [
        str(POOL_BIN),
        "-stratum", "0.0.0.0:3334",
        "-rpc", "127.0.0.1:19334",
        "-proxy", "127.0.0.1:19335",
        "-dashboard", "0.0.0.0:18080",
        "-parent-rpc", f"http://{RPC_USER}:{RPC_PASSWORD}@127.0.0.1:{parent.rpc_port}/",
        "-tracker-address", addresses["BLC"],
        "-share-log", str(ROOT / "logs" / "share-logfile"),
        "-pool-log", str(ROOT / "logs" / "eloipool.log"),
        "-poll", "1s",
        "-work-update", "5s",
        "-base-difficulty", "1",
        "-share-target", "7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "-debug-gotwork",
    ]
    for coin in COINS:
        if not coin.aux_name:
            continue
        command.extend([
            "-aux-name", coin.aux_name,
            "-aux-rpc", f"http://{RPC_USER}:{RPC_PASSWORD}@127.0.0.1:{coin.rpc_port}/",
            "-aux-payout", addresses[coin.ticker],
        ])
    return command


def start_pool(addresses: dict[str, str]) -> None:
    pool_pid = ROOT / "run" / "eloipool.pid"
    if pid_running(read_pid(pool_pid)) or port_open("127.0.0.1", 3334):
        return
    command = pool_command(addresses)
    with (ROOT / "logs" / "eloipool.stdout.log").open("ab") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pool_pid.write_text(f"{proc.pid}\n", encoding="utf-8")


def start_miner() -> None:
    miner_pid = ROOT / "run" / "cpu-miner-loop.pid"
    if pid_running(read_pid(miner_pid)):
        return
    loop_script = ROOT / "run" / "cpu-miner-loop.sh"
    loop_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f"LOG={ROOT / 'logs' / 'cpu-miner.log'}\n"
        "while true; do\n"
        f"  STRATUM_HOST=127.0.0.1 STRATUM_PORT=3334 STRATUM_USER=dex-testnet STRATUM_SHARE_COUNT=1 STRATUM_TARGET_MODE=network python3 {CPU_MINER} >> \"$LOG\" 2>&1 || true\n"
        "  sleep 1\n"
        "done\n",
        encoding="utf-8",
    )
    loop_script.chmod(0o755)
    with (ROOT / "logs" / "cpu-miner-loop.stdout.log").open("ab") as log:
        proc = subprocess.Popen([str(loop_script)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    miner_pid.write_text(f"{proc.pid}\n", encoding="utf-8")


def collect_status() -> dict[str, Any]:
    coins: dict[str, Any] = {}
    for coin in COINS:
        row: dict[str, Any] = {
            "rpcPort": coin.rpc_port,
            "p2pPort": coin.p2p_port,
            "pid": read_pid(coin.pid_file),
            "pidRunning": pid_running(read_pid(coin.pid_file)),
            "rpcOpen": port_open("127.0.0.1", coin.rpc_port),
        }
        if row["rpcOpen"]:
            try:
                info = rpc_call(coin, "getblockchaininfo", timeout=3)
                row["chain"] = info.get("chain")
                row["blocks"] = info.get("blocks")
                row["headers"] = info.get("headers")
                row["softforks"] = info.get("softforks")
            except Exception as exc:
                row["rpcError"] = str(exc)
            try:
                network = rpc_call(coin, "getnetworkinfo", timeout=3)
                row["connections"] = network.get("connections")
                row["connections_in"] = network.get("connections_in")
                row["connections_out"] = network.get("connections_out")
            except Exception as exc:
                row["networkError"] = str(exc)
            try:
                row["deploymentInfo"] = rpc_call(coin, "getdeploymentinfo", timeout=3)
            except Exception as exc:
                row["deploymentInfoError"] = str(exc)
        row["peer"] = {
            "rpcPort": coin.peer_rpc_port,
            "p2pPort": coin.peer_p2p_port,
            "pid": read_pid(coin.peer_pid_file),
            "pidRunning": pid_running(read_pid(coin.peer_pid_file)),
            "rpcOpen": port_open("127.0.0.1", coin.peer_rpc_port),
            "p2pOpen": port_open("127.0.0.1", coin.peer_p2p_port),
        }
        coins[coin.ticker] = row
    pool_pid = ROOT / "run" / "eloipool.pid"
    miner_pid = ROOT / "run" / "cpu-miner-loop.pid"
    return {
        "root": str(ROOT),
        "pool": {
            "pid": read_pid(pool_pid),
            "pidRunning": pid_running(read_pid(pool_pid)),
            "stratumOpen": port_open("127.0.0.1", 3334),
            "dashboard": "http://192.168.1.221:18080",
        },
        "miner": {
            "pid": read_pid(miner_pid),
            "pidRunning": pid_running(read_pid(miner_pid)),
        },
        "coins": coins,
    }


def cmd_start(args: argparse.Namespace) -> int:
    installed = install_binaries(force=args.force_install)
    addresses_path = ROOT / "evidence" / "pool-addresses.json"
    for coin in COINS:
        start_daemon(coin)
    for coin in COINS:
        wait_rpc(coin)
    for coin in COINS:
        start_peer_daemon(coin)
    for coin in COINS:
        wait_peer_ports(coin)
        connect_peer(coin)
    addresses = {coin.ticker: ensure_pool_wallet(coin) for coin in COINS}
    addresses_path.write_text(json.dumps(addresses, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    start_pool(addresses)
    deadline = time.time() + 20
    while time.time() < deadline and not port_open("127.0.0.1", 3334):
        time.sleep(0.5)
    start_miner()
    status = collect_status()
    status["installed"] = installed
    status["addresses"] = addresses
    (ROOT / "evidence" / "status-start.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    status = collect_status()
    (ROOT / "evidence" / "status-latest.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    ok = True
    for name in ("cpu-miner-loop.pid", "eloipool.pid"):
        ok = stop_pid(ROOT / "run" / name) and ok
    for coin in COINS:
        try:
            if port_open("127.0.0.1", coin.peer_rpc_port):
                rpc_call(coin, "stop", timeout=3, peer=True)
        except Exception:
            pass
        ok = stop_pid(coin.peer_pid_file) and ok
    for coin in COINS:
        try:
            if port_open("127.0.0.1", coin.rpc_port):
                rpc_call(coin, "stop", timeout=3)
        except Exception:
            pass
        ok = stop_pid(coin.pid_file) and ok
    print(json.dumps({"ok": ok, "root": str(ROOT)}, indent=2, sort_keys=True))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--force-install", action="store_true")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args(argv)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
