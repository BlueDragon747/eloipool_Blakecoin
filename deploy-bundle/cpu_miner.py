#!/usr/bin/env python3
"""Minimal stratum CPU miner for Blakecoin Eloipool staging smoke tests.

This helper is only for low-difficulty local/private smoke tests. It is useful
for first-run validation of the staged pool, not for serious production
hashrate.

Run with the pool already running on 127.0.0.1:3334.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

# Reuse the pool's own Blake-256 implementation so we are guaranteed to be
# hashing identically to the pool's share validator. Resolve from the current
# monorepo location so the helper stays portable on the build server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blake8 import BLAKE  # noqa: E402


import os as _os
HOST = _os.environ.get('STRATUM_HOST', '127.0.0.1')
PORT = int(_os.environ.get('STRATUM_PORT', '3334'))
USERNAME = _os.environ.get('STRATUM_USER', '783c0d31dbf6d7a5bd3d9f9b8e4b3efef0dbe123')
SHARE_COUNT = int(_os.environ.get('STRATUM_SHARE_COUNT', '1'))
TARGET_MODE = _os.environ.get('STRATUM_TARGET_MODE', 'effective').strip().lower()
SOLVER_PATH = _os.environ.get('STRATUM_SOLVER', '').strip()
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)


def swap32(b):
    return b''.join(b[i + 3:i - 1 if i else None:-1] for i in range(0, len(b), 4))


def dblsha(b):
    return sha256(sha256(b).digest()).digest()


def onesha(b):
    return sha256(b).digest()


def bits_to_target(bits_hex):
    """eloipool's stratumserver.py:319 sends `b2a_hex(bits[::-1])` where `bits`
    is the LE form, so the on-the-wire hex is canonical BE (for example
    '1f00ffff' for a low-difficulty lane)."""
    bits_be = bytes.fromhex(bits_hex)
    exponent = bits_be[0]
    mantissa = int.from_bytes(bits_be[1:4], 'big')
    return mantissa << (8 * (exponent - 3))


class StratumClient:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port))
        self.sock.settimeout(15)
        self.buf = b''
        self.req_id = 0
        self.pending = []
        self.extranonce1 = None
        self.extranonce2_size = None
        self.job = None
        self.network_target = None
        self.share_difficulty = 1.0
        self.share_target = DIFF1_TARGET
        self.effective_target = None

    def _send(self, method, params):
        self.req_id += 1
        msg = json.dumps({'id': self.req_id, 'method': method, 'params': params}).encode() + b'\n'
        self.sock.sendall(msg)
        return self.req_id

    def _send_reply(self, msg_id, result):
        """Reply to pool-side JSON-RPC requests such as client.get_version."""
        msg = json.dumps({'id': msg_id, 'result': result, 'error': None}).encode() + b'\n'
        self.sock.sendall(msg)

    def _recv_line(self, use_pending=True):
        if use_pending and self.pending:
            return self.pending.pop(0)
        while b'\n' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError('connection closed')
            self.buf += chunk
        line, self.buf = self.buf.split(b'\n', 1)
        return json.loads(line)

    def subscribe(self):
        self._send('mining.subscribe', ['cpu_miner/0.1'])
        while True:
            msg = self._recv_line()
            if msg.get('id') == 1 and 'result' in msg:
                # result = [[ [..], ... ], extranonce1_hex, extranonce2_size]
                _subs, en1_hex, en2_size = msg['result']
                self.extranonce1 = bytes.fromhex(en1_hex)
                self.extranonce2_size = en2_size
                print(f'[subscribe] extranonce1={en1_hex} en2_size={en2_size}')
                return

    def authorize(self, username, password=''):
        auth_id = self._send('mining.authorize', [username, password])
        while True:
            # Keep pre-auth notify messages buffered, but do not immediately
            # read them back from self.pending while we're still waiting for
            # the auth reply or we can starve that later response forever.
            msg = self._recv_line(use_pending=False)
            method = msg.get('method')
            if method == 'client.get_version' and msg.get('id') is not None:
                # Some stratum servers probe the miner client version before
                # they finish the authorize flow. If we do not answer here, the
                # helper can keep re-queueing the same request and never reach
                # the auth reply or the first username-specific notify.
                self._send_reply(msg['id'], 'cpu_miner/0.1')
                continue
            if method == 'mining.set_difficulty':
                diff = msg['params'][0]
                self.share_difficulty = float(diff)
                self.share_target = int(DIFF1_TARGET / self.share_difficulty)
                print(f'[set_difficulty] {diff}')
                continue
            if method == 'mining.notify':
                # Some pool builds already send the first usable job before the
                # authorize reply comes back. Keep that notify queued so the
                # miner can begin immediately after auth instead of idling until
                # the next template refresh.
                self.pending.append(msg)
                continue
            if msg.get('id') == auth_id:
                if msg.get('result') is not True:
                    raise RuntimeError(f'authorization failed: {msg}')
                print(f'[authorize] username={username}')
                return auth_id
            self.pending.append(msg)

    def wait_for_job(self):
        """Drain notifications until we have a mining.notify."""
        while True:
            msg = self._recv_line()
            method = msg.get('method')
            if method == 'client.get_version' and msg.get('id') is not None:
                self._send_reply(msg['id'], 'cpu_miner/0.1')
                continue
            if method == 'mining.set_difficulty':
                diff = msg['params'][0]
                self.share_difficulty = float(diff)
                self.share_target = int(DIFF1_TARGET / self.share_difficulty)
                print(f'[set_difficulty] {diff}')
            elif method == 'mining.notify':
                p = msg['params']
                self.job = {
                    'job_id': p[0],
                    'prevhash_swapped_hex': p[1],
                    'coinb1_hex': p[2],
                    'coinb2_hex': p[3],
                    'merkle_branch_hex': p[4],
                    'version_hex': p[5],
                    'nbits_le_hex': p[6],
                    'ntime_be_hex': p[7],
                    'clean_jobs': p[8],
                }
                self.network_target = bits_to_target(self.job['nbits_le_hex'])
                # Two operating modes are useful in QA:
                # - "effective": ordinary share proofs, accept whichever target
                #   is easier between the pool share floor and the live network.
                # - "network": payout proofs, insist on a real network-valid
                #   block solve so fee-funded payouts become deterministic again.
                if TARGET_MODE == 'network':
                    self.effective_target = self.network_target
                else:
                    self.effective_target = max(self.network_target, self.share_target)
                print(f'[notify] job={p[0]} bits={p[6]} ntime={p[7]} branch_len={len(p[4])}')
                return self.job
            elif msg.get('id') and 'result' in msg:
                # Auth response, etc.
                pass

    def submit(self, username, job_id, en2_hex, ntime_hex, nonce_hex):
        submit_id = self._send('mining.submit', [username, job_id, en2_hex, ntime_hex, nonce_hex])
        # Drain replies until we see ours.
        while True:
            msg = self._recv_line()
            if msg.get('method') == 'client.get_version' and msg.get('id') is not None:
                self._send_reply(msg['id'], 'cpu_miner/0.1')
                continue
            if msg.get('id') == submit_id and ('result' in msg or 'error' in msg):
                return msg


def build_header(job, en1, en2, ntime_be_hex, nonce_le):
    """Construct the 80-byte block header for the given job + extranonce + nonce."""
    coinbase = (
        bytes.fromhex(job['coinb1_hex'])
        + en1
        + en2
        + bytes.fromhex(job['coinb2_hex'])
    )
    # Blakecoin txid = single SHA-256 (NOT double like Bitcoin).
    coinbase_txid = onesha(coinbase)

    # Merkle root: walk the branch using double-SHA256 internal combine
    # (Blakecoin's `Hash4` in src/consensus/merkle.cpp uses dbl-SHA256).
    root = coinbase_txid
    for h_hex in job['merkle_branch_hex']:
        root = dblsha(root + bytes.fromhex(h_hex))

    version_le = struct.pack('<L', int(job['version_hex'], 16))
    prevhash_le = swap32(bytes.fromhex(job['prevhash_swapped_hex']))
    ntime_le = bytes.fromhex(ntime_be_hex)[::-1]
    nbits_le = bytes.fromhex(job['nbits_le_hex'])[::-1]  # stratum sends BE-of-LE; want LE
    # Wait - re-derive: stratumserver.py:319 sends b2a_hex(bits[::-1]) where `bits`
    # is the LE form already. So the on-the-wire hex is BE. The header needs LE,
    # so we reverse the bytes once.
    nbits_le = bytes.fromhex(job['nbits_le_hex'])[::-1]

    header = version_le + prevhash_le + root + ntime_le + nbits_le + nonce_le
    assert len(header) == 80, len(header)
    return header, root


def hash_int(h32):
    return int.from_bytes(h32[::-1], 'big')


def solve_with_external_solver(header):
    result = subprocess.run(
        [SOLVER_PATH, header.hex()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f'solver failed: {result.stderr.strip() or result.stdout.strip()}')
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        raise RuntimeError(f'unexpected solver output: {result.stdout!r}')
    nonce = int(parts[0], 10)
    hash_hex = parts[1]
    return nonce, bytes.fromhex(hash_hex)[::-1]


def main():
    cli = StratumClient(HOST, PORT)
    cli.subscribe()
    cli.authorize(USERNAME)
    accepted = 0
    attempts = 0
    base_en2 = int.from_bytes(os.urandom(cli.extranonce2_size), 'big')
    job = None

    while accepted < SHARE_COUNT:
        if job is None:
            job = cli.wait_for_job()
            print(f'[target] network target = 0x{cli.network_target:064x}')
            print(f'[target] share target   = 0x{cli.share_target:064x}')
            print(f'[target] mode          = {TARGET_MODE}')
            print(f'[target] effective tgt  = 0x{cli.effective_target:064x}')

        attempts += 1
        en1 = cli.extranonce1
        en2_int = (base_en2 + accepted + attempts - 1) % (1 << (8 * cli.extranonce2_size))
        en2 = en2_int.to_bytes(cli.extranonce2_size, 'big')
        ntime = job['ntime_be_hex']

        start = time.time()
        found = None
        if SOLVER_PATH:
            header, root = build_header(job, en1, en2, ntime, struct.pack('<L', 0))
            nonce, h = solve_with_external_solver(header)
            if hash_int(h) <= cli.effective_target:
                found = (nonce, h, root)
            else:
                print(f'[solver] found nonce={nonce} hash={h[::-1].hex()} above effective target; waiting for new job')
        else:
            for nonce in range(0, 1 << 32):
                nonce_le = struct.pack('<L', nonce)
                header, root = build_header(job, en1, en2, ntime, nonce_le)
                h = BLAKE(256).digest(header)
                if hash_int(h) <= cli.effective_target:
                    found = (nonce, h, root)
                    break
                if nonce and nonce % 100000 == 0:
                    print(f'[scan] share={accepted + 1}/{SHARE_COUNT} nonce={nonce} elapsed={time.time()-start:.2f}s')

        if not found:
            print('FAIL: nonce space exhausted without solve')
            return 1

        nonce, h, root = found
        elapsed = time.time() - start
        print(f'[SOLVED {accepted + 1}/{SHARE_COUNT}] nonce={nonce} en2={en2.hex()} hash={h[::-1].hex()} merkleroot={root[::-1].hex()} elapsed={elapsed:.4f}s')

        nonce_hex = '%08x' % nonce
        en2_hex = en2.hex()
        print(f'[submit] job_id={job["job_id"]} en2={en2_hex} ntime={ntime} nonce={nonce_hex}')
        reply = cli.submit(USERNAME, job['job_id'], en2_hex, ntime, nonce_hex)
        print(f'[reply] {reply}')

        if reply.get('result') is True:
            accepted += 1
            job = None
            continue

        # The pool likely rolled to a new job while we were scanning; wait for
        # the next notify and keep filling the requested share batch.
        job = None

    print(f'[batch] accepted={accepted}/{SHARE_COUNT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
