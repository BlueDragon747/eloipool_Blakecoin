#!/usr/bin/env python3
"""Fund local and build-server Electrium regnet wallets from pool wallets.

This is an operational helper for the 25.2 DEX regnet lane. It assumes the
six 25.2 daemons are already running on the build-server current lane:
BLC 51320, BBTC 51322, ELT 51324, LIT 51326, PHO 51328, UMO 51330.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path("/mnt/ram-build/dex25-hub-qa-20260526/current-regnet-pool")
LOCAL_ADDRESSES = ROOT / "local-electrium-addresses.json"
EVIDENCE = ROOT / "evidence" / "fund-electrium-wallets.json"
MANIFEST = Path.home() / ".blakestream" / "electrium-hub" / "wallets.json"
CORE_AUTH = base64.b64encode(b"dex-regnet:dex-regnet-pass").decode()

COINS = {
    "BLC": {"core_port": 51320, "electrium_port": 57101, "amount": "25"},
    "BBTC": {"core_port": 51322, "electrium_port": 57102, "amount": "25"},
    "PHO": {"core_port": 51328, "electrium_port": 57105, "amount": "1000"},
    "ELT": {"core_port": 51324, "electrium_port": 57103, "amount": "25"},
    "LIT": {"core_port": 51326, "electrium_port": 57104, "amount": "0.1"},
    "UMO": {"core_port": 51330, "electrium_port": 57106, "amount": "0.001"},
}


def post_json(url: str, auth: str, method: str, params: list[Any], timeout: int = 120) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "1.0", "id": "fund", "method": method, "params": params}).encode(),
        headers={"content-type": "application/json", "authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = json.loads(res.read())
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
    if body.get("error"):
        raise RuntimeError(f"{method} at {url}: {body['error']}")
    return body.get("result")


def core_rpc(port: int, method: str, params: list[Any] | None = None, wallet: str | None = None) -> Any:
    path = f"/wallet/{wallet}" if wallet else ""
    return post_json(f"http://127.0.0.1:{port}{path}", CORE_AUTH, method, params or [])


def electrium_rpc_manifest() -> dict[str, dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text())
    rows: dict[str, dict[str, Any]] = {}
    for row in manifest["variants"]:
        ticker = str(row["ticker"]).upper()
        rows[ticker] = row["config"]
    return rows


def electrium_rpc(ticker: str, method: str, params: list[Any] | None = None) -> Any:
    cfg = electrium_rpc_manifest()[ticker]
    port = int(cfg["rpcPort"])
    auth = base64.b64encode(f"{cfg['rpcUser']}:{cfg['rpcPassword']}".encode()).decode()
    return post_json(f"http://127.0.0.1:{port}", auth, method, params or [], timeout=30)


def main() -> None:
    local = json.loads(LOCAL_ADDRESSES.read_text())
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for ticker, cfg in COINS.items():
        core_port = int(cfg["core_port"])
        amount = str(cfg["amount"])
        build_addr = electrium_rpc(ticker, "createnewaddress")
        pool_addr = core_rpc(core_port, "getnewaddress", ["", "bech32"], wallet="pool")
        tx_local = core_rpc(core_port, "sendtoaddress", [local[ticker], amount], wallet="pool")
        tx_build = core_rpc(core_port, "sendtoaddress", [build_addr, amount], wallet="pool")
        core_rpc(core_port, "generatetoaddress", [1, pool_addr])
        results.append({
            "coin": ticker,
            "amount": amount,
            "localAddress": local[ticker],
            "localTxid": tx_local,
            "buildServerAddress": build_addr,
            "buildServerTxid": tx_build,
            "heightAfterConfirm": core_rpc(core_port, "getblockcount"),
        })
    payload = {"generatedAt": int(time.time()), "results": results}
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
