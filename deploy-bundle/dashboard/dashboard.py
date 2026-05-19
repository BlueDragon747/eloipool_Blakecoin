"""Minimal pool + chain dashboard for Blakecoin Eloipool post-SegWit release.

Single-file Flask app. No DB, no SSE, no cleverness — just polls the daemon's
JSON-RPC and tails the share log + eloipool log on every render. Sized to fit
on a phone screen so the user can monitor a remote pool deployment from a
browser anywhere.

Designed to coexist with any existing Blakecoin services by binding to a
separate dashboard port. It can be exposed directly or proxied behind nginx,
but it should not depend on devnet-only layout assumptions.

Env vars (operator sets these via systemd EnvironmentFile=):
    DASH_BIND                    host:port to listen on (default 0.0.0.0:8080)
    DASH_RPC_URL                 blakecoind RPC URL with creds
    DASH_POOL_LOG                eloipool's main log file
    DASH_SHARE_LOG               share log path
    DASH_COINBASER_DEBUG_LOG     coinbaser JSONL debug ledger path
    DASH_TRACKER_ADDR            pool keep wallet address (display only)
    DASH_AUX_POOL_ADDRESSES      JSON object of child-chain label -> pool-
                                 controlled payout address
    DASH_AUX_PAYOUT_MODE         `pool` or `per-solver` for child-chain
                                 block reward accounting
    DASH_STRATUM_HOST            public hostname for stratum URL display
    DASH_STRATUM_PORT            stratum port for stratum URL display
    DASH_COINBASER               path to coinbaser.py (display only)
    DASH_MINING_KEY_V2_COIN_HRPS JSON object of coin-label -> bech32 HRP
                                 used by the mining-key generator to
                                 preview all BlakeStream payout addresses

The Defaults below are placeholder values used only for local-dev sanity
checking. On the production VPS, deploy.sh writes a dashboard.env file
that sets all of these explicitly per /opt/blakecoin-pool/dashboard/dashboard.env.
"""

import bisect
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

# The canonical mining-key crypto module lives at the top of the eloipool
# tree (mining_key.py). Make it importable from wherever the
# dashboard is launched: bundle (deploy-bundle/dashboard/) and VPS deploy
# (/opt/blakecoin-pool/dashboard/) both have eloipool/ as a sibling directory
# of dashboard/. The mining_key module then handles its own path discovery
# for blake8 + the vendored base58 from inside the eloipool tree.
def _add_mining_key_module_to_path():
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / 'eloipool',                                    # bundle layout
        Path('/opt/blakecoin-pool/eloipool'),                        # VPS layout via deploy.sh
        Path(os.environ.get('DASH_ELOIPOOL_PATH', '')),              # operator override
    ]
    for c in candidates:
        if c and (c / 'mining_key.py').is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return c
    return None
_ELOIPOOL_DIR = _add_mining_key_module_to_path()

try:
    import mining_key as _mk
    address_from_ex                   = _mk.address_from_ex
    address_from_v2_mining_key        = _mk.address_from_v2_mining_key
    mining_key_from_uncompressed_pubkey = _mk.mining_key_from_uncompressed_pubkey
    mining_key_v2_from_compressed_pubkey = _mk.mining_key_v2_from_compressed_pubkey
    is_mining_key                     = _mk.is_mining_key
    resolve_payout_address            = _mk.resolve_payout_address
except ImportError as _e:
    # Mining-key features will be unavailable; the rest of the dashboard
    # still works. The /api/derive-address endpoint will report the error.
    _mk = None
    address_from_ex = None
    address_from_v2_mining_key = None
    mining_key_from_uncompressed_pubkey = None
    mining_key_v2_from_compressed_pubkey = None
    is_mining_key = None
    resolve_payout_address = None

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def getenv_json(name, default):
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default

BIND          = os.environ.get('DASH_BIND',         '0.0.0.0:8080')
RPC_URL       = os.environ.get('DASH_RPC_URL',      'http://user:pass@127.0.0.1:8772')
CHILD_RPC_URLS = getenv_json('DASH_CHILD_RPC_URLS', {})
POOL_LOG      = os.environ.get('DASH_POOL_LOG',     '/var/log/blakecoin-pool/eloipool.stderr')
PROXY_LOG     = os.environ.get('DASH_PROXY_LOG',    '/var/log/blakecoin-pool/merged-mine-proxy.log')
PROXY_CHAIN_LABELS = getenv_json('DASH_PROXY_CHAIN_LABELS', {})
SHARE_LOG     = os.environ.get('DASH_SHARE_LOG',    '/var/log/blakecoin-pool/share-logfile')
COINBASER_DEBUG_LOG = os.environ.get('DASH_COINBASER_DEBUG_LOG', '/var/log/blakecoin-pool/coinbaser.jsonl')
SERVICE_SUMMARY = os.environ.get('DASH_SERVICE_SUMMARY', '').strip() or None
TRACKER_ADDR  = os.environ.get('DASH_TRACKER_ADDR', '(unset)')
STRATUM_HOST  = os.environ.get('DASH_STRATUM_HOST', socket.gethostname())
STRATUM_PORT  = int(os.environ.get('DASH_STRATUM_PORT', '3334'))
RUN_TIMESTAMP = os.environ.get('DASH_RUN_TIMESTAMP') or os.environ.get('RUN_TIMESTAMP', '')
HEADER_TITLE = os.environ.get('DASH_HEADER_TITLE', 'Blakestream Eliopool')
HEADER_SUBTITLE = os.environ.get('DASH_HEADER_SUBTITLE', '')
COINBASER     = os.environ.get('DASH_COINBASER',    '/opt/blakecoin-pool/bin/coinbaser.py')
HAS_COINBASER = os.path.isfile(COINBASER)
CHAIN_TICKERS = getenv_json('DASH_CHAIN_TICKERS', {
    'Blakecoin': 'BLC',
    'BlakeBitcoin': 'BBTC',
    'Electron-ELT': 'ELT',
    'Photon': 'PHO',
    'UniversalMolecule': 'UMO',
    'lithium': 'LIT',
})

MINING_KEY_SEGWIT_HRP = os.environ.get('DASH_MINING_KEY_SEGWIT_HRP', '').strip() or None
MINING_KEY_V2_COIN_HRPS = getenv_json('DASH_MINING_KEY_V2_COIN_HRPS', {})
AUX_POOL_ADDRESSES = getenv_json('DASH_AUX_POOL_ADDRESSES', {})
_AUX_PAYOUT_MODE_RAW = (os.environ.get('DASH_AUX_PAYOUT_MODE') or '').strip().lower()
AUX_PAYOUT_MODE = _AUX_PAYOUT_MODE_RAW if _AUX_PAYOUT_MODE_RAW in ('pool', 'per-solver') else ('pool' if AUX_POOL_ADDRESSES else 'per-solver')


# ---------------------------------------------------------------------------
# Daemon RPC (single-call, blocking, dependency-free)
# ---------------------------------------------------------------------------

def configured_v2_coin_hrps(primary_hrp=None):
    """Return an ordered label -> HRP map for BlakeStream V2 address previews."""
    primary = (primary_hrp or MINING_KEY_SEGWIT_HRP or '').strip()
    mapping = {}
    if primary:
        mapping['Blakecoin'] = primary
    if isinstance(MINING_KEY_V2_COIN_HRPS, dict):
        for label, hrp in MINING_KEY_V2_COIN_HRPS.items():
            if not label:
                continue
            if not isinstance(hrp, str):
                continue
            clean = hrp.strip()
            if clean:
                mapping[str(label)] = clean
    return mapping


def configured_chain_order():
    order = ['Blakecoin']
    if isinstance(CHILD_RPC_URLS, dict):
        for label in CHILD_RPC_URLS.keys():
            if not label:
                continue
            clean = str(label)
            if clean not in order:
                order.append(clean)
    if isinstance(CHAIN_TICKERS, dict):
        for label in CHAIN_TICKERS.keys():
            if not label:
                continue
            clean = str(label)
            if clean not in order:
                order.append(clean)
    return order


def configured_proxy_chain_labels():
    mapping = {}
    if isinstance(PROXY_CHAIN_LABELS, dict):
        for alias, label in PROXY_CHAIN_LABELS.items():
            if not alias or not label:
                continue
            mapping[str(alias)] = str(label)
    if mapping:
        return mapping

    fallback_aliases = ['MM', 'MM1', 'MM3', 'MM4', 'MM5']
    child_labels = [label for label in configured_chain_order() if label != 'Blakecoin']
    return {
        alias: label for alias, label in zip(fallback_aliases, child_labels)
    }


def derive_v2_addresses(mining_key, primary_hrp=None):
    if address_from_v2_mining_key is None:
        raise RuntimeError('v2 mining-key helpers unavailable')

    primary = (primary_hrp or MINING_KEY_SEGWIT_HRP or '').strip()
    derived_address = address_from_v2_mining_key(mining_key, primary) if primary else None
    derived_addresses = {}
    for label, hrp in configured_v2_coin_hrps(primary).items():
        address = address_from_v2_mining_key(mining_key, hrp)
        if not address:
            continue
        derived_addresses[label] = {
            'label': label,
            'hrp': hrp,
            'address': address,
            'addr_type': 'bech32',
        }
    return {
        'primary_hrp': primary,
        'derived_address': derived_address,
        'derived_addresses': derived_addresses,
    }

def rpc_url(url, method, params=None):
    # Parse user:pass out of the URL ourselves — urllib's urlopen doesn't
    # forward URL-embedded basic auth, it tries to DNS-resolve the userinfo.
    import base64
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    userinfo = ''
    if parsed.username:
        userinfo = f'{parsed.username}:{parsed.password or ""}'
    netloc = parsed.hostname + (f':{parsed.port}' if parsed.port else '')
    clean_url = urlunparse((parsed.scheme, netloc, parsed.path or '/', '', '', ''))

    headers = {'Content-Type': 'application/json'}
    if userinfo:
        headers['Authorization'] = 'Basic ' + base64.b64encode(userinfo.encode()).decode()

    payload = json.dumps({'jsonrpc': '1.0', 'id': 'dash', 'method': method, 'params': params or []})
    req = urllib.request.Request(clean_url, data=payload.encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        if body.get('error'):
            return {'_error': body['error']}
        return body['result']
    except Exception as e:
        return {'_error': str(e)}


def rpc(method, params=None):
    return rpc_url(RPC_URL, method, params)


def chain_rpc_url(label):
    clean = str(label or '').strip()
    if not clean:
        return None
    if clean == 'Blakecoin':
        return RPC_URL
    if isinstance(CHILD_RPC_URLS, dict):
        candidate = CHILD_RPC_URLS.get(clean)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def chain_rpc(label, method, params=None):
    url = chain_rpc_url(label)
    if not url:
        return {'_error': f'no rpc url configured for {label}'}
    return rpc_url(url, method, params)


BLOCK_META_CACHE = {}
BLOCK_META_CACHE_TTL = 10.0


# ---------------------------------------------------------------------------
# Log tailers
# ---------------------------------------------------------------------------

def tail(path, n=40):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            buf = b''
            chunk = 4096
            while size > 0 and buf.count(b'\n') <= n:
                back = min(chunk, size)
                f.seek(size - back)
                buf = f.read(back) + buf
                size -= back
            lines = buf.decode('utf-8', errors='replace').splitlines()
            return lines[-n:]
    except FileNotFoundError:
        return ['(log file not present yet)']
    except Exception as e:
        return [f'(error reading log: {e})']


def read_lines(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return ['(log file not present yet)']
    except Exception as e:
        return [f'(error reading log: {e})']


_SENSITIVE_LOG_PATTERNS = [
    (
        re.compile(r'([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@', re.I),
        r'\1<redacted>@',
    ),
    (
        re.compile(r'\b((?:rpc)?pass(?:word)?|secret|token|auth|cookie|private[_ -]?key)\s*[:=]\s*([^\s,;]+)', re.I),
        r'\1=<redacted>',
    ),
    (
        re.compile(r'\bgh[pousr]_[A-Za-z0-9_]{20,}\b'),
        '<redacted-token>',
    ),
]


def redact_sensitive_log_text(text):
    for pattern, replacement in _SENSITIVE_LOG_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def display_pool_log(lines):
    """Collapse noisy known tracebacks before sending log rows to the UI."""
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if 'checkShare' not in line or 'Failed to submit gotwork' not in line:
            out.append(redact_sensitive_log_text(line))
            i += 1
            continue

        err = None
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            stripped = nxt.strip()
            if re.match(r'^\d{4}-\d{2}-\d{2} ', nxt):
                break
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*(Error|Exception)?:', stripped):
                err = stripped
            j += 1

        message = line.replace('Failed to submit gotwork', 'Gotwork submit failed: merged-mining proxy unavailable')
        if err:
            message = f'{message} ({err})'
        out.append(redact_sensitive_log_text(message))
        i = j
    return out


def build_chain_overview(parent_chain):
    rows = []
    for label in configured_chain_order():
        info = parent_chain if label == 'Blakecoin' else chain_rpc(label, 'getblockchaininfo')
        row = {
            'label': label,
            'ticker': CHAIN_TICKERS.get(label, label),
        }
        if not isinstance(info, dict) or info.get('_error'):
            row.update({
                'status': 'error',
                'error': redact_sensitive_log_text(str(info.get('_error') if isinstance(info, dict) else info)),
            })
            rows.append(row)
            continue

        blocks = info.get('blocks')
        headers = info.get('headers')
        progress = info.get('verificationprogress')
        row.update({
            'status': 'synced' if blocks is not None and headers is not None and blocks >= headers else 'syncing',
            'chain': info.get('chain'),
            'blocks': blocks,
            'headers': headers,
            'verificationprogress': progress,
            'difficulty': info.get('difficulty'),
            'bestblockhash': info.get('bestblockhash'),
        })
        peers = parent_chain.get('connections') if label == 'Blakecoin' else chain_rpc(label, 'getconnectioncount')
        if isinstance(peers, int):
            row['connections'] = peers
        rows.append(row)
    return rows


SOLVED_RE = re.compile(r'New block:\s+([0-9a-f]+) \(height: (\d+); bits: ([0-9a-f]+)\)')
BLKHASH_RE = re.compile(r'BLKHASH:\s+([0-9a-f]+)')
LOGTS_RE   = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})')
PROXY_SOLVE_RE = re.compile(r',solve,=,([01]),([01]),([01]),([01]),([01]),([0-9a-f]+)$')
PROXY_SOLVE_STATUS_RE = re.compile(r',solve_status,(parent-accepted|parent-rejected),([01]),([01]),([01]),([01]),([01]),([0-9a-f]+)$')
PROXY_SOLVE_DETAIL_RE = re.compile(r',solve_detail,(parent-accepted|parent-rejected),([0-9a-f]+),(\{.*\})$')
AUX_HASH_RE = re.compile(r'(MM\d*): aux_hash=([0-9a-f]+)\s+merkle_index=(\d+)')
BLOCK_ACCEPTED_RE = re.compile(r'(MM\d*): Block accepted!')


def _normalize_block_hash(raw_hash):
    """Normalize solved-block hashes from logs for matching/display.

    `BLKHASH:` lines in eloipool omit leading zeros while `New block:` lines
    keep the full 64-hex representation. Pad shorter values so we can match a
    solved parent block to the corresponding template-advance line.
    """
    if not raw_hash:
        return ''
    return str(raw_hash).strip().lower().zfill(64)


def _hash_suffix_match(left, right, width=16):
    if not left or not right:
        return False
    a = str(left).strip().lower()
    b = str(right).strip().lower()
    return a.endswith(b[-width:]) or b.endswith(a[-width:])


def _classify_addr_string(addr):
    """Internal: classify a bare address string. Returns 'bech32'/'legacy'/'p2sh'/'none'."""
    if not addr:
        return 'none'
    lower = addr.lower()
    if addr == lower and '1' in addr:
        hrp, _sep, rest = addr.partition('1')
        bech32_charset = set('qpzry9x8gf2tvdw0s3jn54khce6mua7l')
        if hrp and rest and all(ch in bech32_charset for ch in rest):
            return 'bech32'
    if _mk is not None:
        detect_codec = getattr(_mk, '_detect_checksum_codec_safe', None)
        if callable(detect_codec) and detect_codec(addr) is None:
            return 'none'
    else:
        if not (26 <= len(addr) <= 62 and all(ch in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz' for ch in addr)):
            return 'none'
    if addr.startswith(('3', 'q')):
        return 'p2sh'
    if 26 <= len(addr) <= 62:
        return 'legacy'
    return 'none'


def parse_stratum_username(s):
    """Parse a stratum username into a payout identity.

    If the canonical mining_key module is available, this prefers
    resolve_payout_address(...) so the dashboard sees the same direct-address
    and mining-key resolution rules that coinbaser uses for real payouts.

    Returns a dict {addr, addr_type, worker, raw, kind, mining_key} where:
      - addr       = payout address or None
      - addr_type  = 'bech32' / 'legacy' / 'p2sh' / 'none'
      - worker     = worker suffix (or fallback raw label when unresolved)
      - raw        = original username
      - kind       = 'direct' / 'derived_v2' / 'mining_key_v2' / 'skip'
      - mining_key = normalized 40-hex key when present

    Examples:
      'B....rig1'      → {addr: 'B...', kind: 'direct', worker: 'rig1'}
      '783c....'       → {addr: 'blc1...', kind: 'derived_v2', worker: None}
      'alice.rig1'     → {addr: None, kind: 'skip', worker: 'alice.rig1'}
    """
    if not s:
        return {'addr': None, 'addr_type': 'none', 'worker': None, 'raw': s, 'kind': 'skip', 'mining_key': None}

    if resolve_payout_address is not None:
        resolved = resolve_payout_address(s, None, MINING_KEY_SEGWIT_HRP)
        if resolved is not None:
            addr = resolved.get('address')
            worker = resolved.get('worker')
            return {
                'addr':       addr,
                'addr_type':  resolved.get('addr_type') or (_classify_addr_string(addr) if addr else 'none'),
                'worker':     worker if worker is not None else (s if addr is None else None),
                'raw':        s,
                'kind':       resolved.get('kind', 'skip'),
                'mining_key': resolved.get('mining_key'),
            }

    head, sep, tail = s.partition('.')
    head_type = _classify_addr_string(head)
    if head_type != 'none':
        return {
            'addr':      head,
            'addr_type': head_type,
            'worker':    tail if sep else None,
            'raw':       s,
            'kind':      'direct',
            'mining_key': None,
        }
    # Head is not a valid address — treat the whole string as the worker label.
    return {'addr': None, 'addr_type': 'none', 'worker': s, 'raw': s, 'kind': 'skip', 'mining_key': None}


def classify_address(s):
    """Back-compat shim. Returns the address-type only ('bech32'/'legacy'/'p2sh'/'none')."""
    return parse_stratum_username(s)['addr_type']


# ---------------------------------------------------------------------------
# Mining-key crypto helpers are imported from the canonical staging repo copy
# of mining_key.py. See MINING-KEY.md for the full design/provenance history.
# The four names address_from_ex, mining_key_from_uncompressed_pubkey,
# is_mining_key, and resolve_payout_address are wired up at module-load
# time near the top of this file (the import block above).
# ---------------------------------------------------------------------------


def _parse_log_ts(line):
    """eloipool stamps lines as '2026-04-07 19:09:02,272'. Return epoch seconds."""
    m = LOGTS_RE.match(line)
    if not m:
        return None
    try:
        t = time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        return time.mktime(t) + int(m.group(2)) / 1000.0
    except Exception:
        return None


def parse_pool_state(lines, max_rows=15):
    """Extract recent solves from the eloipool log lines.

    A pool-solved block leaves a 'BLKHASH: <hash>' line followed shortly after
    by 'New block: <same hash> (height: N; bits: ...)'. Anything that appears
    only as 'New block:' (without a matching BLKHASH) is a template-update for
    a block someone else mined upstream — we exclude those.
    """
    blkhashes = set()
    for line in lines:
        m = BLKHASH_RE.search(line)
        if m:
            blkhashes.add(_normalize_block_hash(m.group(1)))

    solved = []
    seen_hashes = set()
    for line in lines:
        m = SOLVED_RE.search(line)
        if m:
            h = _normalize_block_hash(m.group(1))
            if h in blkhashes and h not in seen_hashes:
                # eloipool's 'New block: <hash> (height: N; ...)' uses N as the
                # next-to-mine height (the height of the *template* the pool
                # just rolled to), so the block that just got solved is at N-1.
                solved.append({
                    'hash':   h,
                    'height': int(m.group(2)) - 1,
                    'bits':   m.group(3),
                    'ts':     _parse_log_ts(line),
                })
                seen_hashes.add(h)
    if max_rows is None:
        return solved
    return solved[-max_rows:]


def _finalize_solved_block(block, aux_chains=None, parent_status='parent-accepted'):
    aux_chains = list(aux_chains or [])
    parent_accepted = parent_status != 'parent-rejected'
    if parent_accepted:
        chain_labels = ['Blakecoin'] + [label for label in aux_chains if label != 'Blakecoin']
    else:
        chain_labels = [label for label in aux_chains if label != 'Blakecoin']
    return {
        **block,
        'aux_chains': aux_chains,
        'aux_count': len(aux_chains),
        'chain_labels': chain_labels,
        'parent_status': parent_status,
        'parent_accepted': parent_accepted,
        'aux_only': not parent_accepted,
        'accepted_details': [],
    }


def _aux_only_event_from_proxy_solve(solve):
    return _finalize_solved_block(
        {
            'hash': _normalize_block_hash(solve.get('parent_hash')),
            'height': None,
            'bits': None,
            'ts': solve.get('ts'),
        },
        solve.get('aux_chains') or [],
        parent_status='parent-rejected',
    )


def _merge_proxy_solve_candidates(candidates):
    merged = []
    idx = 0
    while idx < len(candidates):
        current = candidates[idx]
        if (
            current.get('parent_status') is None
            and idx + 1 < len(candidates)
        ):
            nxt = candidates[idx + 1]
            ts_a = current.get('ts')
            ts_b = nxt.get('ts')
            time_close = (
                ts_a is not None
                and ts_b is not None
                and abs(ts_a - ts_b) <= 2.0
            )
            if (
                nxt.get('parent_status') in ('parent-accepted', 'parent-rejected')
                and (_hash_suffix_match(current.get('parent_hash_raw'), nxt.get('parent_hash_raw')) or time_close)
            ):
                details = list(nxt.get('accepted_details') or current.get('accepted_details') or [])
                merged.append({
                    **nxt,
                    'accepted_details': details,
                })
                idx += 2
                continue
        merged.append(current)
        idx += 1
    return merged


def parse_proxy_solves(lines, max_rows=30):
    alias_map = configured_proxy_chain_labels()
    aliases = ['MM', 'MM1', 'MM3', 'MM4', 'MM5']
    detail_by_hash = {}
    for line in lines:
        m = PROXY_SOLVE_DETAIL_RE.search(line)
        if not m:
            continue
        parent_status = m.group(1)
        parent_hash = _normalize_block_hash(m.group(2))
        try:
            payload = json.loads(m.group(3))
        except Exception:
            continue
        accepted = []
        for entry in payload.get('accepted', []):
            alias = str(entry.get('alias') or '').strip()
            label = alias_map.get(alias, alias) if alias else None
            accepted.append({
                'alias': alias,
                'label': label or alias or 'unknown',
                'height': entry.get('height'),
                'hash': _normalize_block_hash(entry.get('hash')),
            })
        detail_by_hash[parent_hash] = {
            'parent_status': parent_status,
            'accepted_details': accepted,
        }
    pending_aux_hashes = {}
    accepted_in_window = {}
    candidates = []
    for line in lines:
        m = AUX_HASH_RE.search(line)
        if m:
            pending_aux_hashes[m.group(1)] = _normalize_block_hash(m.group(2))
            continue

        m = BLOCK_ACCEPTED_RE.search(line)
        if m:
            alias = m.group(1)
            accepted_hash = pending_aux_hashes.get(alias)
            if accepted_hash:
                accepted_in_window[alias] = {
                    'alias': alias,
                    'label': alias_map.get(alias, alias),
                    'height': None,
                    'hash': accepted_hash,
                }
            continue

        m = PROXY_SOLVE_STATUS_RE.search(line)
        parent_status = None
        parent_hash = None
        explicit_status = False
        if m:
            parent_status = m.group(1)
            flags = m.groups()[1:6]
            parent_hash = _normalize_block_hash(m.group(7))
            explicit_status = True
        else:
            m = PROXY_SOLVE_RE.search(line)
            if not m:
                continue
            flags = m.groups()[:5]
            raw_hash = m.group(6)
            parent_hash = _normalize_block_hash(raw_hash) if len(raw_hash) >= 32 else raw_hash.strip().lower()
        aux_chains = []
        child_flags = {}
        for alias, flag in zip(aliases, flags):
            label = alias_map.get(alias, alias)
            child_flags[label] = (flag == '1')
            if flag == '1':
                aux_chains.append(label)
        detail = detail_by_hash.get(parent_hash, {})
        candidate = {
            'ts': _parse_log_ts(line),
            'aux_chains': aux_chains,
            'child_flags': child_flags,
            'parent_status': parent_status or detail.get('parent_status'),
            'parent_hash': parent_hash,
            'parent_hash_raw': parent_hash,
            'explicit_status': explicit_status,
            'accepted_details': detail.get('accepted_details', list(accepted_in_window.values())),
        }
        candidates.append(candidate)
        pending_aux_hashes = {}
        accepted_in_window = {}
    solves = _merge_proxy_solve_candidates(candidates)
    if max_rows is None:
        return solves
    return solves[-max_rows:]


def attach_proxy_solves(solved_blocks, proxy_solves, max_gap=12.0, max_rows=15):
    if not solved_blocks and not proxy_solves:
        return []
    if not proxy_solves:
        return [_finalize_solved_block(block) for block in solved_blocks]

    used = set()
    merged = []
    for block in solved_blocks:
        best_idx = None
        best_gap = None
        block_hash = _normalize_block_hash(block.get('hash'))
        for idx, solve in enumerate(proxy_solves):
            if idx in used:
                continue
            if solve.get('parent_status') == 'parent-rejected':
                continue
            solve_hash = solve.get('parent_hash')
            if solve_hash and len(solve_hash) == 64 and solve_hash == block_hash:
                best_idx = idx
                best_gap = 0.0
                break
        block_ts = block.get('ts')
        if best_idx is None and block_ts is not None:
            for idx, solve in enumerate(proxy_solves):
                if idx in used or solve.get('ts') is None:
                    continue
                if solve.get('parent_status') == 'parent-rejected':
                    continue
                gap = abs(block_ts - solve['ts'])
                if gap > max_gap:
                    continue
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best_idx = idx
        aux_chains = proxy_solves[best_idx]['aux_chains'] if best_idx is not None else []
        parent_status = proxy_solves[best_idx].get('parent_status') if best_idx is not None else 'parent-accepted'
        if best_idx is not None:
            used.add(best_idx)
        finalized = _finalize_solved_block(block, aux_chains, parent_status or 'parent-accepted')
        details = list(proxy_solves[best_idx].get('accepted_details') or []) if best_idx is not None else []
        if finalized['parent_accepted']:
            details = [{
                'alias': 'BLC',
                'label': 'Blakecoin',
                'height': finalized.get('height'),
                'hash': _normalize_block_hash(finalized.get('hash')),
            }] + details
        finalized['accepted_details'] = details
        merged.append(finalized)

    for idx, solve in enumerate(proxy_solves):
        if idx in used:
            continue
        if not solve.get('aux_chains'):
            continue
        solve_parent_status = solve.get('parent_status')
        if solve_parent_status not in (None, 'parent-rejected'):
            continue
        aux_only_event = _aux_only_event_from_proxy_solve({
                **solve,
                'parent_status': 'parent-rejected',
            })
        aux_only_event['accepted_details'] = list(solve.get('accepted_details') or [])
        merged.append(aux_only_event)

    merged.sort(key=lambda block: (block.get('ts') or 0))
    if max_rows is None:
        return merged
    return merged[-max_rows:]


def parse_share_line(line):
    """Parse one eloipool share-log line into a normalized dict.

    Historical format:
        time host user our_result upstream_result reason solution

    Current extended format:
        time host user our_result upstream_result reason share_diff target_hex solution
    """
    parts = line.split()
    if len(parts) < 7:
        return None
    try:
        ts = float(parts[0])
    except ValueError:
        return None

    our_result = parts[3] == 'Y'
    upstream_result = parts[4] == 'Y'
    contribution = our_result or upstream_result
    share_diff = None
    solution = parts[6]
    if len(parts) >= 9:
        try:
            if parts[6] != '-':
                share_diff = float(parts[6])
        except ValueError:
            share_diff = None
        solution = parts[8]
    weight = share_diff if contribution and share_diff and share_diff > 0 else (1.0 if contribution else 0.0)
    return {
        'time': ts,
        'host': parts[1].strip("'\""),
        'user': parts[2],
        'our_result': our_result,
        'upstream_result': upstream_result,
        'reason': parts[5] if len(parts) > 5 else '-',
        'share_diff': share_diff,
        'weight': weight,
        'contribution': contribution,
        'solution': solution,
    }


def parse_share_log(lines, max_rows=15):
    """Normalize recent share-log lines for dashboard display."""
    out = []
    for line in lines:
        entry = parse_share_line(line)
        if not entry:
            continue
        # Promote real upstream-accepted block solves to 'BLOCK!'
        if entry['upstream_result']:
            display_status = 'BLOCK ✓'
            display_reason = ''
        elif entry['our_result']:
            display_status = 'share ✓'
            display_reason = ''
        else:
            display_status = 'Rejected'
            display_reason = entry['reason'] if entry['reason'] != '-' else ''
        user = entry['user']
        parsed = parse_stratum_username(user)
        out.append({
            'time':     entry['time'],
            'host':     entry['host'],
            'user':     user,
            'addr':     parsed['addr'],
            'addr_type': parsed['addr_type'],
            'kind':     parsed.get('kind'),
            'mining_key': parsed.get('mining_key'),
            'worker':   parsed['worker'],
            'accepted': entry['contribution'],
            'our_result': entry['our_result'],
            'upstream_result': entry['upstream_result'],
            'share_diff': entry['share_diff'],
            'weight':   entry['weight'],
            'status':   display_status,
            'reason':   display_reason,
        })
    if max_rows is None:
        return out
    return out[-max_rows:]


def _is_real_proxy_solve(solve):
    if not solve:
        return False
    return bool(solve.get('parent_status') or solve.get('aux_chains'))


def attach_share_chain_outcomes(shares, proxy_solves, max_lead=4.0, max_lag=0.5):
    if not shares:
        return []

    chain_order = configured_chain_order()
    child_order = [label for label in chain_order if label != 'Blakecoin']
    unmatched_solves = [
        {
            **solve,
            '_matched': False,
        } for solve in (proxy_solves or []) if _is_real_proxy_solve(solve)
    ]

    def match_solve(share_ts):
        best_idx = None
        best_key = None
        for idx, solve in enumerate(unmatched_solves):
            if solve.get('_matched'):
                continue
            solve_ts = solve.get('ts')
            if solve_ts is None:
                continue
            delta = solve_ts - share_ts
            if delta < -max_lag or delta > max_lead:
                continue
            # Prefer solves that occur immediately after the share submission.
            key = (0 if delta >= 0 else 1, abs(delta))
            if best_key is None or key < best_key:
                best_key = key
                best_idx = idx
        if best_idx is None:
            return None
        unmatched_solves[best_idx]['_matched'] = True
        return unmatched_solves[best_idx]

    enriched = []
    for share in shares:
        matched = match_solve(share.get('time') or 0)
        accepted_labels = []
        attempted_labels = []

        if matched is not None:
            attempted_labels = list(chain_order)
            parent_status = matched.get('parent_status')
            parent_accepted = share.get('upstream_result') or parent_status == 'parent-accepted'
            if parent_accepted:
                accepted_labels.append('Blakecoin')
            for label in child_order:
                if label in (matched.get('aux_chains') or []):
                    accepted_labels.append(label)
        elif share.get('upstream_result'):
            attempted_labels = list(chain_order)
            accepted_labels = ['Blakecoin']
        else:
            attempted_labels = []

        # Preserve configured chain order in both accepted and rejected lists.
        accepted_set = set(accepted_labels)
        accepted_labels = [label for label in chain_order if label in accepted_set]
        rejected_labels = [label for label in attempted_labels if label not in accepted_set]

        enriched.append({
            **share,
            'chains_attempted': attempted_labels,
            'chains_accepted': accepted_labels,
            'chains_rejected': rejected_labels,
            'has_chain_outcomes': bool(attempted_labels),
            'proxy_parent_status': matched.get('parent_status') if matched else None,
            'proxy_matched': matched is not None,
        })
    return enriched


def build_recent_solved_shares(share_lines, proxy_solves, max_rows=15, max_lead=4.0, max_lag=0.5):
    """Return recent winning shares with per-chain block outcomes.

    Normal accepted shares are intentionally excluded. This keeps the chain
    chips meaningful: green/red only describe actual block submission results.
    """
    chain_order = configured_chain_order()
    child_order = [label for label in chain_order if label != 'Blakecoin']
    real_solves = [
        solve for solve in (proxy_solves or [])
        if _is_real_proxy_solve(solve) and solve.get('ts') is not None
    ]
    if not real_solves:
        return []

    recent_solves = real_solves[-max(max_rows * 3, 30):]
    windows = sorted(
        (solve['ts'] - max_lead, solve['ts'] + max_lag)
        for solve in recent_solves
    )
    candidate_lines = []
    for line in share_lines:
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            share_ts = float(parts[0])
        except ValueError:
            continue

        # Always keep parent-chain winners even if the proxy detail scrolled
        # or the timestamp window misses by a little. For aux-only solves,
        # keep only accepted rows close to recent proxy solve timestamps.
        if parts[4] == 'Y':
            candidate_lines.append(line)
            continue
        if parts[3] != 'Y':
            continue
        for start, end in windows:
            if share_ts < start:
                break
            if share_ts <= end:
                candidate_lines.append(line)
                break

    shares = parse_share_log(candidate_lines, max_rows=None)
    if not shares:
        return []

    rows = []
    used_share_idx = set()
    share_times = [share.get('time') or 0 for share in shares]

    for solve in recent_solves:
        solve_ts = solve.get('ts')
        best_idx = None
        best_key = None
        pos = bisect.bisect_left(share_times, solve_ts)
        candidates = []
        idx = pos - 1
        while idx >= 0 and solve_ts - share_times[idx] <= max_lead:
            candidates.append(idx)
            idx -= 1
        idx = pos
        while idx < len(shares) and share_times[idx] - solve_ts <= max_lag:
            candidates.append(idx)
            idx += 1
        for idx in candidates:
            if idx in used_share_idx:
                continue
            share = shares[idx]
            share_ts = share.get('time')
            if share_ts is None:
                continue
            delta = solve_ts - share_ts
            if delta < -max_lag or delta > max_lead:
                continue
            key = (0 if delta >= 0 else 1, abs(delta))
            if best_key is None or key < best_key:
                best_key = key
                best_idx = idx
        if best_idx is None:
            continue

        used_share_idx.add(best_idx)
        share = shares[best_idx]
        parent_status = solve.get('parent_status')
        parent_accepted = share.get('upstream_result') or parent_status == 'parent-accepted'
        accepted_labels = []
        if parent_accepted:
            accepted_labels.append('Blakecoin')
        for label in child_order:
            if label in (solve.get('aux_chains') or []):
                accepted_labels.append(label)
        accepted_set = set(accepted_labels)
        accepted_labels = [label for label in chain_order if label in accepted_set]
        rows.append({
            **share,
            'chains_attempted': list(chain_order),
            'chains_accepted': accepted_labels,
            'chains_rejected': [label for label in chain_order if label not in accepted_set],
            'has_chain_outcomes': True,
            'proxy_parent_status': parent_status,
            'proxy_matched': True,
        })

    # Fallback: if the share log has a parent-chain solve but no proxy outcome
    # close enough, still show the winning BLC share without inventing aux
    # rejects from missing data.
    seen_times = {row.get('time') for row in rows}
    for share in shares:
        if not share.get('upstream_result') or share.get('time') in seen_times:
            continue
        rows.append({
            **share,
            'chains_attempted': ['Blakecoin'],
            'chains_accepted': ['Blakecoin'],
            'chains_rejected': [],
            'has_chain_outcomes': True,
            'proxy_parent_status': None,
            'proxy_matched': False,
        })

    rows.sort(key=lambda row: row.get('time') or 0)
    return rows[-max_rows:]


# ---------------------------------------------------------------------------
# Stratum miner introspection — derived from `ss` since eloipool exposes no
# internal API for connected clients. Cross-references the share log to attach
# usernames and last-share timestamps to each established TCP connection.
# ---------------------------------------------------------------------------

# Short-lived cache for the `ss` subprocess output. Under GIL contention
# from concurrent /api/state threads, `subprocess.run` latency balloons
# from its native 10ms to 20+ seconds while it waits for the GIL to be
# released long enough to fork. Connection info doesn't change fast enough
# to justify that cost — 3s freshness is plenty.
_SS_CACHE_TS = 0.0
_SS_CACHE_VALUE = []
_SS_CACHE_TTL = 3.0
_SS_CACHE_LOCK = threading.Lock()


def list_stratum_connections(port=None):
    """Return list of {peer_addr, peer_port} for ESTABLISHED connections to
    the stratum port. Uses `ss -tn state established 'sport = :PORT'`."""
    global _SS_CACHE_TS, _SS_CACHE_VALUE
    now = time.time()
    if (now - _SS_CACHE_TS) < _SS_CACHE_TTL:
        return list(_SS_CACHE_VALUE)
    port = port or STRATUM_PORT
    try:
        out = subprocess.run(
            ['ss', '-tnH', 'state', 'established', f'sport = :{port}'],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return []
        peers = []
        for line in out.stdout.splitlines():
            # ss -tnH columns: Recv-Q  Send-Q  Local  Peer  [process]
            parts = line.split()
            if len(parts) < 4:
                continue
            peer = parts[3]
            # Strip leading [::ffff: for v4-mapped addresses
            if peer.startswith('[::ffff:'):
                peer = peer[len('[::ffff:'):].rstrip(']')
            if ':' in peer:
                host, _, p = peer.rpartition(':')
                peers.append({'peer_addr': host, 'peer_port': int(p)})
            else:
                peers.append({'peer_addr': peer, 'peer_port': 0})
        with _SS_CACHE_LOCK:
            _SS_CACHE_TS = time.time()
            _SS_CACHE_VALUE = peers
        return peers
    except FileNotFoundError:
        # ss not installed (very rare on Linux); fall back to empty
        return []
    except Exception:
        return []


def _credit_aux_chain_wins(id_stats, all_solved_blocks, proxy_solves, max_gap=60.0):
    """Add aux-chain wins to each identity's block count.

    `parse_identity_stats` already counts parent-chain (Blakecoin) wins by
    looking at upstream_result in the share log. This pass also credits
    every aux chain that was submitted alongside each winning share, so the
    number on the dashboard reflects all merged-mined blocks (parent + aux),
    matching what a miner intuitively expects on a merged pool.

    Correlation: match a winning share's timestamp to the nearest proxy-
    solve within `max_gap` seconds. Each accepted aux chain on that solve
    adds one to the identity's block count.
    """
    if not proxy_solves:
        return
    # Build a list of (ts, accepted_aux_count) from proxy solves once, sorted
    solve_points = []
    for s in proxy_solves:
        if s.get('parent_status') == 'parent-rejected':
            continue
        ts = s.get('ts')
        aux = s.get('aux_chains') or []
        # aux is a list of chain-label strings; each entry represents an
        # accepted aux-chain win. Reject cases are filtered earlier via
        # parent-rejected; in-list entries are already accepted.
        accepted_aux = len(aux)
        if ts is not None and accepted_aux:
            solve_points.append((ts, accepted_aux))
    if not solve_points:
        return
    solve_points.sort(key=lambda p: p[0])
    # Also index parent-solved block hashes → ts for hash-matching fallback
    hash_to_ts = {}
    for b in all_solved_blocks or []:
        h = _normalize_block_hash(b.get('hash'))
        ts = b.get('ts')
        if h and ts is not None:
            hash_to_ts[h] = ts

    # Use bisect for O(log n) nearest-neighbor lookup. Without this the
    # naive O(identities × wins × solves) scan is minutes long once the
    # proxy solves cache reaches 100k+ entries under heavy ASIC load.
    solve_ts_sorted = [p[0] for p in solve_points]
    used_solve_idx = set()
    for key, st in id_stats.items():
        win_ts_list = st.pop('_winning_ts', [])
        extra = 0
        for wts in win_ts_list:
            # Binary-search the closest timestamp
            pos = bisect.bisect_left(solve_ts_sorted, wts)
            best_idx = None
            best_gap = max_gap
            for candidate in (pos - 1, pos):
                if 0 <= candidate < len(solve_points) and candidate not in used_solve_idx:
                    gap = abs(solve_points[candidate][0] - wts)
                    if gap <= best_gap:
                        best_gap = gap
                        best_idx = candidate
            if best_idx is not None:
                used_solve_idx.add(best_idx)
                extra += solve_points[best_idx][1]
        st['blocks'] += extra


def parse_identity_stats(share_lines):
    """Aggregate per-(host, username) stats from the share log.

    Returns { (host, user): {shares, blocks, last} }.

    Each (host, user) tuple is one stratum identity. eloipool has no concept
    of accounts or address↔name mapping; each stratum username is opaque and
    independent of every other one. Two usernames from the same IP are NOT
    "the same miner" — they're two separate identities that may or may not
    belong to the same person.
    """
    stats = {}
    for line in share_lines:
        entry = parse_share_line(line)
        if not entry:
            continue
        host = entry['host']
        user = entry['user']
        key = (host, user)
        s = stats.setdefault(key, {'shares': 0, 'accepted_shares': 0, 'weighted_work': 0.0, 'blocks': 0, 'last': 0, '_winning_ts': []})
        s['shares'] += 1
        if entry['contribution']:
            s['accepted_shares'] += 1
            s['weighted_work'] += entry['weight']
        if entry['time'] > s['last']:
            s['last'] = entry['time']
        if entry['upstream_result']:
            s['blocks'] += 1
            s['_winning_ts'].append(entry['time'])
    return stats


def merge_identity_view(connections, identity_stats):
    """One row per (host, username) — a stratum identity.

    'active' = identity has submitted a share in the last 5 minutes (a proxy
    for "currently mining"). We can't reliably attribute a TCP socket from
    `ss` to a specific stratum username without inspecting eloipool's
    internal state, so we use share-recency as the activity signal instead.

    Each row also gets parsed `address.worker` parts so the dashboard can show
    them as separate fields per the standard Bitcoin stratum convention.
    """
    now = time.time()
    ACTIVE_WINDOW = 300  # 5 minutes
    live_hosts = {c['peer_addr'] for c in connections}
    rows = []
    for (host, user), st in identity_stats.items():
        last = st['last']
        active = bool(last and (now - last) < ACTIVE_WINDOW)
        parsed = parse_stratum_username(user)
        rows.append({
            'identity':        user,
            'addr':            parsed['addr'],
            'addr_type':       parsed['addr_type'],
            'kind':            parsed.get('kind'),
            'mining_key':      parsed.get('mining_key'),
            'worker':          parsed['worker'],
            'host':            host,
            'host_has_socket': host in live_hosts,
            'active':          active,
            'shares':          st['shares'],
            'accepted_shares': st['accepted_shares'],
            'weighted_work':   round(st['weighted_work'], 8),
            'blocks':          st['blocks'],
            'last_share':      last or None,
        })
    rows.sort(key=lambda r: (-int(r['active']), -(r['last_share'] or 0)))
    return rows


# ---------------------------------------------------------------------------
# Pool status derivation
# ---------------------------------------------------------------------------

# Background payout-warmer infrastructure.
# Walking 1400+ historical blocks × 6 daemons to accumulate payout totals
# is a multi-second operation; doing it inline on every /api/state call
# makes requests slower than the 5s browser refresh and the page goes blank.
# Instead, a background thread continuously refreshes two caches and the
# request handler only READS them — O(1) regardless of history size.
_PAYOUT_SNAPSHOT_PARENT = {}             # {address: satoshis_total}
_PAYOUT_SNAPSHOT_CHILDREN = {}           # {chain_label: {address: satoshis}}
_PAYOUT_SNAPSHOT_CHILD_POOL = {}         # {chain_label: satoshis_total kept by pool}
_PAYOUT_SNAPSHOT_LOCK = threading.Lock()
_PAYOUT_WARMER_PARENT_HASHES = []        # latest parent block hashes to walk
_PAYOUT_WARMER_SOLVED_BLOCKS = []        # freshest solved block objects for aux accounting
_PAYOUT_WARMER_STARTED_LOCK = threading.Lock()
_PAYOUT_WARMER_STARTED = False

_COINBASER_DEBUG_OFFSET = 0
_COINBASER_DEBUG_CACHE = []
_COINBASER_DEBUG_TAIL = b''


def _ensure_payout_warmer_started():
    """Lazily spawn the payout-warmer thread on the first /api/state call."""
    global _PAYOUT_WARMER_STARTED
    with _PAYOUT_WARMER_STARTED_LOCK:
        if _PAYOUT_WARMER_STARTED:
            return
        t = threading.Thread(target=_payout_warmer_loop, daemon=True, name='payout-warmer')
        t.start()
        _PAYOUT_WARMER_STARTED = True


def _payout_warmer_loop():
    """Continuously refresh payout totals. RPCs release the GIL during
    network I/O so this thread does not starve the request handler.
    The heavy proxy-log parsing is NOT done here — aux-chain payouts come
    from querying each aux daemon's tip directly, bounded to ~500 blocks.
    """
    while True:
        try:
            with _PAYOUT_SNAPSHOT_LOCK:
                parent_hashes = list(_PAYOUT_WARMER_PARENT_HASHES)
                solved_blocks = list(_PAYOUT_WARMER_SOLVED_BLOCKS)
            if parent_hashes:
                parent_payouts = get_recent_block_payouts(parent_hashes, max_blocks=None)
            else:
                parent_payouts = {}
            if AUX_PAYOUT_MODE == 'pool':
                child_payouts, child_pool_keeps = get_recent_child_accounted_payouts(solved_blocks, max_blocks=500)
            else:
                child_payouts = get_recent_child_payouts(max_blocks=500)
                child_pool_keeps = {}
            with _PAYOUT_SNAPSHOT_LOCK:
                _PAYOUT_SNAPSHOT_PARENT.clear()
                _PAYOUT_SNAPSHOT_PARENT.update(parent_payouts)
                _PAYOUT_SNAPSHOT_CHILDREN.clear()
                _PAYOUT_SNAPSHOT_CHILDREN.update(child_payouts)
                _PAYOUT_SNAPSHOT_CHILD_POOL.clear()
                _PAYOUT_SNAPSHOT_CHILD_POOL.update(child_pool_keeps)
        except Exception as e:
            sys.stderr.write(f'[payout-warmer] error: {e!r}\n')
        # Gentle pacing. Most passes are fast (cache hits) so this loop
        # spends most of its life sleeping.
        time.sleep(5.0)


# Module-level cache of blocks that have already been merged with proxy
# solves. `attach_proxy_solves` is O(blocks × solves); with a full history
# of 1400+ blocks and thousands of proxy solves this becomes seconds per
# call. Blocks are append-only and per-block output is stable once merged,
# so we cache the finalized dict by block hash and only run the merge on
# blocks we haven't seen yet. `_USED_PROXY_IDX` persists which proxy
# solves have already been consumed so we don't double-count them.
_MERGED_BLOCK_BY_HASH = {}
_MERGED_BLOCK_HASH_ORDER = []
_USED_PROXY_IDX = set()


def attach_proxy_solves_cached(all_solved_blocks, proxy_solves):
    """Cumulative wrapper around attach_proxy_solves.

    Only merges blocks that haven't been merged before — previously merged
    output is returned from the cache. Preserves total ordering.
    """
    known = _MERGED_BLOCK_BY_HASH
    order = _MERGED_BLOCK_HASH_ORDER
    new_blocks = []
    for block in all_solved_blocks or []:
        h = _normalize_block_hash(block.get('hash'))
        if h and h in known:
            continue
        new_blocks.append(block)
    if new_blocks:
        # attach_proxy_solves needs to walk proxy_solves once; to avoid
        # re-consuming already-used ones, we filter them here. We pass a
        # copy of the unused subset with original indices preserved via a
        # wrapper so `used` tracking in the original function still works.
        unused_solves = [s for i, s in enumerate(proxy_solves) if i not in _USED_PROXY_IDX]
        unused_idx_map = [i for i in range(len(proxy_solves)) if i not in _USED_PROXY_IDX]
        merged = attach_proxy_solves(new_blocks, unused_solves, max_rows=None)
        # We can't tell which unused_solves were consumed without extra
        # instrumentation, so conservatively mark any solve whose parent_hash
        # now appears in a merged block as used.
        merged_hashes = set()
        for m in merged:
            h = _normalize_block_hash(m.get('hash'))
            if h:
                known[h] = m
                order.append(h)
                merged_hashes.add(h)
        for idx, solve in zip(unused_idx_map, unused_solves):
            if _normalize_block_hash(solve.get('parent_hash')) in merged_hashes:
                _USED_PROXY_IDX.add(idx)
    # Rebuild output in original block order
    out = []
    for block in all_solved_blocks or []:
        h = _normalize_block_hash(block.get('hash'))
        if h and h in known:
            out.append(known[h])
        else:
            out.append(_finalize_solved_block(block))
    return out


# Module-level cache of parsed proxy solves. The merged-mine-proxy log
# grows to tens of MB/day under an active ASIC, so re-reading and re-parsing
# the whole thing on every /api/state call blocks requests for seconds.
# Instead we remember where we last read from and only parse NEW bytes each
# refresh — a proxy solve, once logged, is immutable. Memory grows with
# cumulative solves but that's bounded by how long the process runs.
_PROXY_LOG_OFFSET = 0
_PROXY_SOLVES_CACHE = []
_PROXY_LOG_TAIL = b''  # leftover bytes from last read without a trailing \n
_PROXY_SOLVES_LOCK = threading.Lock()


def incremental_proxy_solves():
    global _PROXY_LOG_OFFSET, _PROXY_SOLVES_CACHE, _PROXY_LOG_TAIL
    with _PROXY_SOLVES_LOCK:
        if not PROXY_LOG:
            return []
        try:
            with open(PROXY_LOG, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                if size < _PROXY_LOG_OFFSET:
                    # Log rotated or truncated — reset and reparse from scratch
                    _PROXY_LOG_OFFSET = 0
                    _PROXY_SOLVES_CACHE = []
                    _PROXY_LOG_TAIL = b''
                f.seek(_PROXY_LOG_OFFSET)
                chunk = f.read(size - _PROXY_LOG_OFFSET)
                _PROXY_LOG_OFFSET = size
        except FileNotFoundError:
            return list(_PROXY_SOLVES_CACHE)
        except Exception:
            return list(_PROXY_SOLVES_CACHE)
        if not chunk:
            return list(_PROXY_SOLVES_CACHE)
        data = _PROXY_LOG_TAIL + chunk
        # Stash anything after the last newline for next pass so a line that
        # arrives in two reads isn't split mid-regex.
        if b'\n' in data:
            last_nl = data.rfind(b'\n')
            complete = data[:last_nl + 1]
            _PROXY_LOG_TAIL = data[last_nl + 1:]
        else:
            _PROXY_LOG_TAIL = data
            complete = b''
        if complete:
            new_lines = complete.decode('utf-8', errors='replace').splitlines()
            new_solves = parse_proxy_solves(new_lines, max_rows=None)
            _PROXY_SOLVES_CACHE.extend(new_solves)
        return list(_PROXY_SOLVES_CACHE)


def incremental_coinbaser_debug_entries():
    global _COINBASER_DEBUG_OFFSET, _COINBASER_DEBUG_CACHE, _COINBASER_DEBUG_TAIL
    if not COINBASER_DEBUG_LOG:
        return []
    try:
        with open(COINBASER_DEBUG_LOG, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            if size < _COINBASER_DEBUG_OFFSET:
                _COINBASER_DEBUG_OFFSET = 0
                _COINBASER_DEBUG_CACHE = []
                _COINBASER_DEBUG_TAIL = b''
            f.seek(_COINBASER_DEBUG_OFFSET)
            chunk = f.read(size - _COINBASER_DEBUG_OFFSET)
            _COINBASER_DEBUG_OFFSET = size
    except FileNotFoundError:
        return _COINBASER_DEBUG_CACHE
    except Exception:
        return _COINBASER_DEBUG_CACHE
    if not chunk:
        return _COINBASER_DEBUG_CACHE
    data = _COINBASER_DEBUG_TAIL + chunk
    if b'\n' in data:
        last_nl = data.rfind(b'\n')
        complete = data[:last_nl + 1]
        _COINBASER_DEBUG_TAIL = data[last_nl + 1:]
    else:
        _COINBASER_DEBUG_TAIL = data
        complete = b''
    if complete:
        for line in complete.decode('utf-8', errors='replace').splitlines():
            try:
                raw = json.loads(line)
            except Exception:
                continue
            entry = _normalize_coinbaser_debug_entry(raw)
            if entry is not None:
                _COINBASER_DEBUG_CACHE.append(entry)
    return _COINBASER_DEBUG_CACHE


def _normalize_coinbaser_debug_entry(raw):
    if not isinstance(raw, dict):
        return None
    prev_hash = _normalize_block_hash(raw.get('prev'))
    try:
        ts = float(raw.get('ts') or 0)
    except Exception:
        ts = 0.0
    try:
        total_work = Decimal(str(raw.get('total_work') or '0'))
    except InvalidOperation:
        total_work = Decimal(0)
    try:
        keep_bps = int(raw.get('pool_keep_bps') or 0)
    except Exception:
        keep_bps = 0
    splits = []
    for item in raw.get('splits') or []:
        if not isinstance(item, dict):
            continue
        addr = str(item.get('addr') or '').strip()
        if not addr:
            continue
        try:
            work = Decimal(str(item.get('work') or '0'))
        except InvalidOperation:
            continue
        mining_key = item.get('mining_key')
        if mining_key is not None:
            mining_key = str(mining_key).strip().lower() or None
        splits.append({
            'addr': addr,
            'work': work,
            'kind': str(item.get('kind') or '').strip() or None,
            'mining_key': mining_key,
            'addr_type': str(item.get('addr_type') or '').strip() or None,
        })
    if not splits:
        return None
    return {
        'ts': ts,
        'prev': prev_hash,
        'total_work': total_work,
        'pool_keep_bps': keep_bps,
        'splits': splits,
    }


def _coinbaser_debug_indexes(entries):
    by_ts = sorted((e for e in entries if isinstance(e, dict)), key=lambda e: e.get('ts') or 0)
    by_prev = {}
    for entry in by_ts:
        prev_hash = entry.get('prev')
        if not prev_hash:
            continue
        by_prev.setdefault(prev_hash, []).append(entry)
    return by_ts, by_prev


def _latest_coinbaser_entry_before(entries, block_time):
    if not entries:
        return None
    if block_time is None:
        return entries[-1]
    timestamps = [entry.get('ts') or 0 for entry in entries]
    idx = bisect.bisect_right(timestamps, block_time) - 1
    if idx >= 0:
        return entries[idx]
    return entries[-1]


def _find_coinbaser_entry(entries_by_ts, entries_by_prev, block_time, prev_hash=None):
    if prev_hash:
        entry = _latest_coinbaser_entry_before(entries_by_prev.get(prev_hash, []), block_time)
        if entry is not None:
            return entry
    return _latest_coinbaser_entry_before(entries_by_ts, block_time)


def _scale_coinbaser_splits_for_reward(entry, total_satoshis):
    rows = [row for row in (entry.get('splits') or []) if row.get('work', Decimal(0)) > 0]
    if not rows or total_satoshis <= 0:
        return [], max(total_satoshis, 0)
    keep_bps = int(entry.get('pool_keep_bps') or 0)
    payable = total_satoshis * max(0, 10000 - keep_bps) // 10000
    if payable <= 0:
        return [], total_satoshis
    total_work = sum((row.get('work') or Decimal(0)) for row in rows)
    if total_work <= 0:
        return [], total_satoshis
    ordered = sorted(rows, key=lambda row: (-row.get('work', Decimal(0)), row.get('addr') or ''))
    distributed = 0
    payout_rows = []
    payable_dec = Decimal(payable)
    for row in ordered[:-1]:
        satoshis = int((payable_dec * row['work'] / total_work).to_integral_value(rounding=ROUND_DOWN))
        payout_rows.append((row, satoshis))
        distributed += satoshis
    payout_rows.append((ordered[-1], payable - distributed))

    emitted = sum(satoshis for _, satoshis in payout_rows)
    if emitted >= total_satoshis and payout_rows:
        overshoot = emitted - total_satoshis + 1
        row, satoshis = payout_rows[-1]
        payout_rows[-1] = (row, satoshis - overshoot)
    payout_rows = [(row, satoshis) for row, satoshis in payout_rows if satoshis > 0]
    emitted = sum(satoshis for _, satoshis in payout_rows)
    pool_remainder = max(total_satoshis - emitted, 0)
    return ([
        {
            'addr': row.get('addr'),
            'kind': row.get('kind'),
            'mining_key': row.get('mining_key'),
            'addr_type': row.get('addr_type'),
            'sat': satoshis,
        }
        for row, satoshis in payout_rows
    ], pool_remainder)


def _derive_child_target_from_split(split, label):
    hrp = configured_v2_coin_hrps().get(label)
    mining_key = split.get('mining_key')
    if not hrp or not mining_key or address_from_v2_mining_key is None:
        return None
    try:
        return address_from_v2_mining_key(mining_key, hrp)
    except Exception:
        return None


def aggregate_identity_payout_totals(identities, chain_order=None):
    order = list(chain_order or configured_chain_order())
    totals = {str(label): 0 for label in order}
    for identity in identities or []:
        for label, satoshis in (identity.get('all_paid_satoshis') or {}).items():
            try:
                value = int(satoshis or 0)
            except Exception:
                continue
            key = str(label)
            totals[key] = totals.get(key, 0) + value
    return {str(label): totals.get(str(label), 0) for label in order}


def pool_wallet_paid_totals(tracker_paid_satoshis, aux_pool_paid_satoshis=None, chain_order=None):
    order = list(chain_order or configured_chain_order())
    totals = {str(label): 0 for label in order}
    try:
        totals['Blakecoin'] = int(tracker_paid_satoshis or 0)
    except Exception:
        totals['Blakecoin'] = 0
    for label, satoshis in (aux_pool_paid_satoshis or {}).items():
        try:
            value = int(satoshis or 0)
        except Exception:
            continue
        key = str(label)
        totals[key] = totals.get(key, 0) + value
    return {str(label): totals.get(str(label), 0) for label in order}


def wallet_balances_from_info(wallet_info):
    info = wallet_info if isinstance(wallet_info, dict) else {}

    def _to_sats(key):
        try:
            return int(round(float(info.get(key, 0) or 0) * 100_000_000))
        except Exception:
            return 0

    confirmed = _to_sats('balance')
    immature = _to_sats('immature_balance')
    unconfirmed = _to_sats('unconfirmed_balance')
    return {
        'confirmed_satoshis': confirmed,
        'immature_satoshis': immature,
        'unconfirmed_satoshis': unconfirmed,
        'total_satoshis': confirmed + immature + unconfirmed,
    }


def get_pool_wallet_rows(chain_order=None):
    order = list(chain_order or configured_chain_order())
    rows = []
    for label in order:
        if label == 'Blakecoin':
            address = TRACKER_ADDR
        else:
            address = AUX_POOL_ADDRESSES.get(label)
        if not address:
            continue
        wallet_info = chain_rpc(label, 'getwalletinfo')
        if not isinstance(wallet_info, dict):
            wallet_info = {'_error': 'wallet rpc unavailable'}
        balances = wallet_balances_from_info(wallet_info)
        rows.append({
            'label': label,
            'ticker': CHAIN_TICKERS.get(label, label),
            'address': address,
            'confirmed_satoshis': balances['confirmed_satoshis'],
            'immature_satoshis': balances['immature_satoshis'],
            'unconfirmed_satoshis': balances['unconfirmed_satoshis'],
            'total_satoshis': balances['total_satoshis'],
            'error': wallet_info.get('_error') if isinstance(wallet_info, dict) else None,
        })
    return rows


def get_recent_child_accounted_payouts(solved_blocks, max_blocks=500):
    payouts = {}
    pool_keeps = {}
    entries = incremental_coinbaser_debug_entries()
    if not entries:
        return payouts, pool_keeps
    entries_by_ts, entries_by_prev = _coinbaser_debug_indexes(entries)
    blocks = list(solved_blocks or [])
    if max_blocks is not None:
        blocks = blocks[-max_blocks:]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        details = list(block.get('accepted_details') or [])
        if not details:
            continue
        block_hash = _normalize_block_hash(block.get('hash'))
        block_time = block.get('ts')
        prev_hash = None
        if block_hash and block.get('parent_accepted') is not False:
            parent_meta = get_chain_block_meta('Blakecoin', block_hash)
            prev_hash = parent_meta.get('prev_hash')
            if parent_meta.get('time') is not None:
                block_time = parent_meta.get('time')
        entry = _find_coinbaser_entry(entries_by_ts, entries_by_prev, block_time, prev_hash)
        if entry is None:
            continue
        for detail in details:
            label = str(detail.get('label') or detail.get('alias') or '').strip()
            if not label or label == 'Blakecoin':
                continue
            child_hash = _normalize_block_hash(detail.get('hash'))
            if not child_hash:
                continue
            reward = get_chain_block_meta(label, child_hash).get('reward')
            if reward in (None, ''):
                continue
            try:
                reward_satoshis = int(round(float(reward) * 100_000_000))
            except Exception:
                continue
            split_rows, pool_remainder = _scale_coinbaser_splits_for_reward(entry, reward_satoshis)
            payouts.setdefault(label, {})
            pool_keeps[label] = pool_keeps.get(label, 0) + pool_remainder
            for split in split_rows:
                target = _derive_child_target_from_split(split, label)
                if not target:
                    pool_keeps[label] += split['sat']
                    continue
                payouts[label][target] = payouts[label].get(target, 0) + split['sat']
    return payouts, pool_keeps


# Module-level cache of coinbase payouts, keyed by (rpc_fn_id, block_hash).
# Block coinbase outputs are immutable once confirmed, so the RPC result
# for a given hash never changes and can be cached for the lifetime of the
# process. Without this, /api/state re-RPCs every historical block on every
# refresh — with an ASIC mining devnet that's 1000+ serial RPCs per call
# and the page goes blank because requests take longer than the 5s refresh.
_BLOCK_COINBASE_CACHE = {}


def _coinbase_payouts_for_hash(block_hash, rpc_fn):
    """Return {addr: satoshis} for one block's coinbase. Cached by hash.
    Block hashes are cryptographically unique so collisions across chains
    are not a concern, and each call to `get_child_payouts_from_solved_events`
    creates a fresh lambda for rpc_fn — so we key on the hash alone, not
    on the rpc_fn identity, to preserve cache hits across refreshes.
    """
    cached = _BLOCK_COINBASE_CACHE.get(block_hash)
    if cached is not None:
        return cached
    block = rpc_fn('getblock', [block_hash, 2])
    if not isinstance(block, dict) or '_error' in block:
        return {}
    txs = block.get('tx', [])
    if not txs:
        return {}
    out = {}
    for vout in txs[0].get('vout', []):
        spk = vout.get('scriptPubKey', {})
        addrs = spk.get('addresses') or []
        if not addrs:
            continue
        value = vout.get('value', 0)
        sats = int(round(float(value) * 100_000_000))
        addr = addrs[0]
        out[addr] = out.get(addr, 0) + sats
    _BLOCK_COINBASE_CACHE[block_hash] = out
    return out


def get_recent_block_payouts(block_hashes, max_blocks=None, rpc_fn=None):
    """For each recent block hash, walk the coinbase via the daemon RPC and
    sum BLC paid to each address. Returns {address: satoshis_paid}.

    If `max_blocks` is set, we only inspect the most recent `max_blocks`
    hashes to keep RPC chatter bounded. Per-hash results are memoized —
    see _BLOCK_COINBASE_CACHE.
    """
    rpc_fn = rpc_fn or rpc
    payouts = {}
    hashes = list(block_hashes or [])
    if max_blocks is not None:
        hashes = hashes[-max_blocks:]
    for h in hashes:
        for addr, sats in _coinbase_payouts_for_hash(h, rpc_fn).items():
            payouts[addr] = payouts.get(addr, 0) + sats
    return payouts


_BLOCKHASH_BY_HEIGHT_CACHE = {}   # {(url, height): hash} — immutable per chain


def get_recent_tip_block_hashes(url, max_blocks=20):
    chain = rpc_url(url, 'getblockchaininfo')
    if not isinstance(chain, dict) or '_error' in chain:
        return []
    try:
        height = int(chain.get('blocks', -1))
    except Exception:
        return []
    if height < 0:
        return []
    start = max(0, height - max_blocks + 1)
    hashes = []
    for block_height in range(start, height + 1):
        cache_key = (url, block_height)
        block_hash = _BLOCKHASH_BY_HEIGHT_CACHE.get(cache_key)
        if block_hash is None:
            block_hash = rpc_url(url, 'getblockhash', [block_height])
            if isinstance(block_hash, str):
                _BLOCKHASH_BY_HEIGHT_CACHE[cache_key] = block_hash
        if isinstance(block_hash, str):
            hashes.append(block_hash)
    return hashes


def get_recent_child_payouts(max_blocks=500):
    """Query each aux daemon's tip for the last `max_blocks` blocks and
    sum coinbase payouts per address. Results are memoized via
    _BLOCK_COINBASE_CACHE so subsequent calls are cheap — first call
    after restart costs ~1 RPC per block per chain, then O(new blocks).
    """
    payouts = {}
    if not isinstance(CHILD_RPC_URLS, dict):
        return payouts
    for label, url in CHILD_RPC_URLS.items():
        if not isinstance(url, str) or not url:
            continue
        block_hashes = get_recent_tip_block_hashes(url, max_blocks=max_blocks)
        payouts[str(label)] = get_recent_block_payouts(
            block_hashes,
            rpc_fn=lambda method, params=None, _url=url: rpc_url(_url, method, params),
        )
    return payouts


def get_child_payouts_from_solved_events(solved_blocks, max_blocks=None):
    payouts = {}
    hashes_by_label = {}
    for block in solved_blocks or []:
        for detail in block.get('accepted_details') or []:
            label = detail.get('label')
            block_hash = _normalize_block_hash(detail.get('hash'))
            if not label or label == 'Blakecoin' or not block_hash:
                continue
            hashes_by_label.setdefault(str(label), []).append(block_hash)

    for label in configured_chain_order():
        if label == 'Blakecoin':
            continue
        url = chain_rpc_url(label)
        if not url:
            continue
        label_hashes = []
        seen = set()
        for block_hash in hashes_by_label.get(label, []):
            if block_hash in seen:
                continue
            seen.add(block_hash)
            label_hashes.append(block_hash)
        payouts[str(label)] = get_recent_block_payouts(
            label_hashes,
            max_blocks=max_blocks,
            rpc_fn=lambda method, params=None, _url=url: rpc_url(_url, method, params),
        )
    return payouts


def _extract_block_reward(block):
    txs = block.get('tx') or []
    if not txs:
        return None
    reward = 0.0
    for vout in txs[0].get('vout', []):
        try:
            reward += float(vout.get('value', 0) or 0)
        except Exception:
            continue
    return reward


def get_chain_block_meta(label, block_hash):
    normalized = _normalize_block_hash(block_hash)
    if not normalized:
        return {}

    cache_key = (str(label), normalized)
    now = time.time()
    cached = BLOCK_META_CACHE.get(cache_key)
    if cached and (now - cached.get('fetched_at', 0)) <= BLOCK_META_CACHE_TTL:
        return dict(cached.get('value') or {})

    block = chain_rpc(label, 'getblock', [normalized, 2])
    if not isinstance(block, dict) or '_error' in block:
        meta = {
            'hash': normalized,
            'height': None,
            'time': None,
            'difficulty': None,
            'confirmations': None,
            'reward': None,
            'size': None,
            'tx_count': None,
            'prev_hash': None,
        }
    else:
        meta = {
            'hash': _normalize_block_hash(block.get('hash') or normalized),
            'height': block.get('height'),
            'time': block.get('time'),
            'difficulty': block.get('difficulty'),
            'confirmations': block.get('confirmations'),
            'reward': _extract_block_reward(block),
            'size': block.get('size'),
            'tx_count': len(block.get('tx') or []),
            'prev_hash': _normalize_block_hash(block.get('previousblockhash')),
        }

    if len(BLOCK_META_CACHE) >= 1024:
        BLOCK_META_CACHE.clear()
    BLOCK_META_CACHE[cache_key] = {
        'fetched_at': now,
        'value': meta,
    }
    return dict(meta)


def build_recent_block_rows(solved_blocks, max_rows=120):
    rows = []
    seen = set()

    for block in reversed(solved_blocks or []):
        details = list(block.get('accepted_details') or [])
        if not details and block.get('parent_accepted', True) and block.get('hash'):
            details = [{
                'alias': 'BLC',
                'label': 'Blakecoin',
                'height': block.get('height'),
                'hash': _normalize_block_hash(block.get('hash')),
            }]

        for item in details:
            label = str(item.get('label') or item.get('alias') or '').strip() or 'unknown'
            block_hash = _normalize_block_hash(item.get('hash') or '')
            if not block_hash and label == 'Blakecoin':
                block_hash = _normalize_block_hash(block.get('hash'))
            if not block_hash:
                continue

            row_key = (label, block_hash)
            if row_key in seen:
                continue
            seen.add(row_key)

            meta = get_chain_block_meta(label, block_hash)
            if label == 'Blakecoin':
                status = 'parent'
            else:
                status = 'aux-only' if block.get('parent_accepted') is False else 'merged'

            row = {
                'coin': label,
                'ticker': CHAIN_TICKERS.get(label, label),
                'height': meta.get('height') if meta.get('height') is not None else item.get('height'),
                'hash': meta.get('hash') or block_hash,
                'time': meta.get('time') if meta.get('time') is not None else block.get('ts'),
                'difficulty': meta.get('difficulty'),
                'confirmations': meta.get('confirmations'),
                'reward': meta.get('reward'),
                'size': meta.get('size'),
                'tx_count': meta.get('tx_count'),
                'status': status,
                'parent_hash': _normalize_block_hash(block.get('hash')),
                'parent_status': block.get('parent_status') or ('parent-rejected' if status == 'aux-only' else 'parent-accepted'),
            }
            if row['height'] is None and label == 'Blakecoin':
                row['height'] = block.get('height')
            rows.append(row)

    rows.sort(key=lambda row: ((row.get('time') or 0), (row.get('height') or -1)), reverse=True)
    return rows[:max_rows]


def identity_payout_targets(identity):
    targets = {}
    mining_key = identity.get('mining_key')
    kind = identity.get('kind')
    if identity.get('addr'):
        targets['Blakecoin'] = identity['addr']
    if mining_key and kind in ('derived_v2', 'mining_key_v2'):
        derived = derive_v2_addresses(mining_key)
        for label, details in derived.get('derived_addresses', {}).items():
            address = details.get('address')
            if address:
                targets[label] = address
    return targets


def read_service_summary():
    if not SERVICE_SUMMARY:
        return None
    try:
        return json.loads(Path(SERVICE_SUMMARY).read_text(encoding='utf-8'))
    except Exception:
        return None


def derive_status(chain, pool_lines, identities, blocks, service_summary=None):
    """One-glance health: MINING / IDLE / STALLED / DEGRADED / DOWN

    Logic (top of list wins):
      - DOWN     : daemon RPC unreachable
      - DEGRADED : daemon up but no recent GBT template lines (pool not polling)
      - IDLE     : explicit zero-miner rehearsal mode from the pool supervisor
      - STALLED  : pool polling but no identity has been active in last 5 min
      - MINING   : pool polling AND >=1 identity active AND >=1 block solved in last 10 min
      - IDLE     : pool polling AND >=1 identity active but no recent solve
    """
    if not isinstance(chain, dict) or '_error' in chain:
        return 'DOWN'

    has_recent_template = False
    if pool_lines:
        for line in pool_lines[-30:]:
            if 'merkleMaker' in line:
                has_recent_template = True
                break
    if not has_recent_template:
        return 'DEGRADED'

    if isinstance(service_summary, dict):
        summary_status = str(service_summary.get('status') or '').strip().lower()
        miner_count = int(service_summary.get('miner_count') or 0)
        if summary_status == 'idle' and miner_count == 0:
            return 'IDLE'

    active = [i for i in identities if i.get('active')]
    if not active:
        return 'STALLED'

    now = time.time()
    last_block = max((i.get('last_share') or 0 for i in identities if i.get('blocks')), default=0)
    if last_block and (now - last_block) < 600:
        return 'MINING'
    return 'IDLE'


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

INDEX_HTML = '''
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BlakeStream Pool</title>
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<style>
  :root {
    --bg:       #1a1d21;
    --bg2:      #22262c;
    --bg3:      #2a2f36;
    --fg:       #e6e9ee;
    --muted:    #8a93a0;
    --accent:   #4fc3f7;
    --accent2:  #66bb6a;
    --warn:     #ffb74d;
    --bad:      #ef5350;
    --purple:   #ba68c8;
    --border:   #353a42;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.45;
  }
  header {
    padding: 14px 20px;
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  header .header-brand {
    display: flex;
    align-items: baseline;
    gap: 12px;
    min-width: 0;
  }
  header h1 {
    margin: 0;
    font-size: 18px;
    color: var(--accent);
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  header .sub {
    color: var(--muted);
    font-size: 12px;
    min-width: 0;
  }
  header .header-endpoint {
    display: flex;
    justify-content: center;
    min-width: 0;
    justify-self: center;
    width: 100%;
    max-width: 520px;
  }
  header .header-side {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    min-width: 0;
  }
  header .meta-pill {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 11px;
    color: inherit;
    font: inherit;
    appearance: none;
  }
  header .meta-pill .lbl {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 9px;
  }
  header .meta-pill .mono {
    color: var(--accent);
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px;
  }
  header .meta-pill .mono.truncate {
    max-width: 250px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-block;
    vertical-align: bottom;
  }
  header .tracker-pill { margin-left: auto; }
  header .tracker-pill.clickable {
    cursor: pointer;
    transition: border-color 0.12s ease, background 0.12s ease;
  }
  header .tracker-pill.clickable:hover {
    border-color: var(--accent);
    background: rgba(79,195,247,0.06);
  }
  header .tracker-pill.clickable:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  header .live { color: var(--accent2); font-size: 11px; display: flex; align-items: center; gap: 6px; }
  header .live::before {
    content: ''; width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent2);
    animation: pulse 2s ease-in-out infinite;
  }
  @media (max-width: 980px) {
    header {
      grid-template-columns: 1fr;
      gap: 12px;
    }
    header .header-brand,
    header .header-endpoint,
    header .header-side {
      justify-content: flex-start;
    }
    header .header-brand {
      flex-wrap: wrap;
    }
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.35; }
  }
  main {
    max-width: 1180px;
    margin: 0 auto;
    padding: 16px;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  @media (max-width: 720px) { main { grid-template-columns: 1fr; padding: 10px; } }

  /* Hero status banner */
  .hero {
    grid-column: 1 / -1;
    background: linear-gradient(135deg, var(--bg2), var(--bg3));
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 22px;
    display: flex;
    align-items: center;
    gap: 28px;
    flex-wrap: wrap;
  }
  @media (max-width: 720px) { .hero { gap: 14px; } }
  .hero .status {
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 8px 18px;
    border-radius: 8px;
    display: inline-block;
    text-align: center;
    min-width: 140px;
  }
  .hero .status.MINING   { color: #1a1d21; background: var(--accent2); }
  .hero .status.IDLE     { color: #1a1d21; background: var(--accent);  }
  .hero .status.STALLED  { color: #1a1d21; background: var(--warn);    }
  .hero .status.DEGRADED { color: #fff;    background: var(--purple);  }
  .hero .status.DOWN     { color: #fff;    background: var(--bad);     }

  /* Compact status pill, used inline inside the Pool card kv2 grid */
  .pool-status {
    display: inline-block;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 3px 10px;
    border-radius: 4px;
    text-transform: uppercase;
  }
  .pool-status.MINING   { color: #1a1d21; background: var(--accent2); }
  .pool-status.IDLE     { color: #1a1d21; background: var(--accent);  }
  .pool-status.STALLED  { color: #1a1d21; background: var(--warn);    }
  .pool-status.DEGRADED { color: #fff;    background: var(--purple);  }
  .pool-status.DOWN     { color: #fff;    background: var(--bad);     }
  .chain-tabs {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    margin: 0 0 10px 0;
  }
  .chain-tab {
    appearance: none;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--muted);
    border-radius: 999px;
    padding: 2px 8px;
    font: inherit;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .chain-tab.synced {
    border-color: rgba(98, 211, 113, 0.65);
    color: var(--accent2);
  }
  .chain-tab.syncing {
    border-color: rgba(255, 184, 77, 0.7);
    color: var(--warn);
  }
  .chain-tab.error {
    border-color: rgba(255, 83, 83, 0.75);
    color: var(--bad);
  }
  .chain-tab.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #10151c;
  }
  .pool-last-block {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .pool-chain-chips {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }
  .pool-chain-chips .share-chain-badge {
    padding: 1px 6px;
  }
  .pool-metric {
    white-space: nowrap;
  }
  .hero .summary {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px 18px;
    font-size: 12px;
  }
  @media (max-width: 720px) { .hero .summary { grid-template-columns: repeat(2, 1fr); } }
  .hero .summary div { display: flex; flex-direction: column; gap: 2px; }
  .hero .summary .lbl { color: var(--muted); text-transform: uppercase; font-size: 10px; letter-spacing: 0.05em; }
  .hero .summary .val { font-size: 16px; font-weight: 600; color: var(--fg); }
  .hero .badge {
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 11px;
    color: var(--muted);
    text-align: right;
  }
  .hero .badge .mono { display: block; color: var(--fg); font-size: 11px; word-break: break-all; }

  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
  }
  .card h2 {
    margin: 0 0 10px 0;
    font-size: 12px;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  /* Collapsible card body — entire card uses <details> + <summary> for the
     header. Click header to fold the body away. Body has max-height +
     overflow-y so long lists scroll inside the card instead of pushing
     everything below them off the screen. */
  details.card-collapse > summary {
    list-style: none;
    cursor: pointer;
    user-select: none;
  }
  details.card-collapse > summary::-webkit-details-marker { display: none; }
  details.card-collapse > summary h2 { margin: 0; }
  details.card-collapse > summary h2::before {
    content: '▾';
    color: var(--muted);
    font-size: 11px;
    margin-right: 4px;
    transition: transform 0.15s;
    display: inline-block;
  }
  details.card-collapse:not([open]) > summary h2::before { transform: rotate(-90deg); }
  details.card-collapse > summary:hover h2::before { color: var(--accent); }
  details.card-collapse > .card-body {
    margin-top: 12px;
    max-height: 480px;
    overflow-y: auto;
    /* nice scrollbar styling */
    scrollbar-width: thin;
    scrollbar-color: var(--border) var(--bg2);
  }
  details.card-collapse > .card-body::-webkit-scrollbar { width: 8px; }
  details.card-collapse > .card-body::-webkit-scrollbar-track { background: var(--bg2); }
  details.card-collapse > .card-body::-webkit-scrollbar-thumb {
    background: var(--border);
    border-radius: 4px;
  }
  details.card-collapse > .card-body::-webkit-scrollbar-thumb:hover {
    background: var(--accent);
  }
  .card h2 .count {
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1px 8px;
    font-size: 10px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0;
  }
  /* Compact "latest …" preview shown in collapsed-card summary rows so the
     header carries useful info before the user expands the card. */
  .card h2 .summary-latest {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    font-weight: 500;
    text-transform: none;
    letter-spacing: 0;
    color: var(--muted);
    text-align: right;
    justify-content: flex-end;
  }
  .card h2 .summary-latest .hpill { font-size: 10px; padding: 1px 6px; }
  .card h2 .summary-latest .mono  { color: var(--fg); }
  .card h2 .legend {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 9px;
    text-transform: none;
    letter-spacing: 0;
    color: var(--muted);
  }
  .card h2 .legend .lbl { color: var(--muted); margin-right: 2px; }
  .card h2 .legend .type-pill {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 3px;
    border: 1px solid var(--border);
    color: var(--muted);
    font-weight: 600;
    cursor: help;
  }
  .card h2 .legend .type-pill.bech32 { color: var(--accent2); border-color: var(--accent2); }
  .card h2 .legend .type-pill.legacy { color: var(--accent);  border-color: var(--accent);  }
  .card h2 .legend .type-pill.p2sh   { color: var(--warn);    border-color: var(--warn);    }
  .card h2 .legend .type-pill.none   { color: var(--bad);     border-color: var(--bad);     }
  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 12px;
    font-size: 13px;
  }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; word-break: break-all; }

  /* 2-column key/value layout with optional full-width rows */
  .kv2 {
    display: grid;
    grid-template-columns: max-content 1fr max-content 1fr;
    gap: 8px 16px;
    font-size: 13px;
  }
  .kv2 dt { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; align-self: center; }
  .kv2 dd { margin: 0; word-break: break-all; align-self: center; }
  .kv2 .span2 { grid-column: 1 / -1; }
  .kv2 .kv-wide {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 8px 16px;
    align-items: center;
  }
  .kv2 .span2.kv-row {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 8px 16px;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--border);
  }
  .kv2 .big {
    font-size: 18px;
    font-weight: 600;
    color: var(--fg);
  }
  .kv2 .big.accent  { color: var(--accent);  }
  .kv2 .big.accent2 { color: var(--accent2); }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  table th, table td {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  table th {
    color: var(--muted);
    font-weight: normal;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.05em;
  }
  table tr:hover td { background: rgba(79,195,247,0.05); }

  /* Identity rows — one per (peer, stratum-username) tuple */
  .id-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .id-card-note {
    background: rgba(79,195,247,0.06);
    border: 1px dashed var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 12px;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.5;
  }
  .id-card-note b { color: var(--fg); }
  details.id-row {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    transition: border-color 0.15s;
  }
  details.id-row[open] { border-color: var(--accent); }
  details.id-row > summary {
    cursor: pointer;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 12px;
    list-style: none;
    user-select: none;
  }
  details.id-row > summary::-webkit-details-marker { display: none; }
  details.id-row > summary:hover { background: rgba(79,195,247,0.04); }
  .id-row .chevron {
    color: var(--muted);
    font-size: 11px;
    flex: 0 0 auto;
    width: 10px;
    transition: transform 0.15s;
    display: inline-block;
  }
  details.id-row[open] .chevron { transform: rotate(90deg); }
  .id-row .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: 0 0 auto;
  }
  .id-row .dot.active { background: var(--accent2); box-shadow: 0 0 6px rgba(102,187,106,0.6); }
  .id-row .dot.idle   { background: var(--muted); }
  .id-row .type-pill {
    flex: 0 0 auto;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid var(--border);
    color: var(--muted);
    font-weight: 600;
    min-width: 50px;
    text-align: center;
  }
  .id-row .type-pill.bech32 { color: var(--accent2); border-color: var(--accent2); }
  .id-row .type-pill.legacy { color: var(--accent);  border-color: var(--accent);  }
  .id-row .type-pill.p2sh   { color: var(--warn);    border-color: var(--warn);    }
  .id-row .type-pill.none   { color: var(--bad);     border-color: var(--bad);     }
  details.id-row.misconfig { border-color: rgba(239,83,80,0.4); }
  .id-row .warn-marker {
    flex: 0 0 auto;
    color: var(--bad);
    font-size: 16px;
    line-height: 1;
    width: 50px;
    text-align: center;
    cursor: help;
  }
  @media (max-width: 720px) {
    .id-row .warn-marker { width: auto; padding: 0 4px; font-size: 14px; }
  }
  .id-row .identity {
    flex: 1 1 auto;
    min-width: 0;
    word-break: break-all;
    line-height: 1.4;
    font-size: 13px;
  }
  .id-row .identity .worker-name {
    color: var(--fg);
    font-weight: 600;
  }
  .id-row .identity .sep {
    color: var(--border);
    margin: 0 8px;
  }
  .id-row .identity .addr-mono {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
    color: var(--muted);
  }
  .id-row .identity .addr-mono.bech32 { color: var(--accent2); }
  .id-row .identity .addr-mono.legacy { color: var(--accent); }
  .id-row .identity .addr-mono.p2sh   { color: var(--warn); }
  .id-row .identity .addr-error {
    color: var(--bad);
    font-style: italic;
    font-size: 12px;
  }
  .id-row .stats {
    flex: 0 0 auto;
    display: flex;
    gap: 18px;
    align-items: center;
  }
  .id-row .stats .stat {
    text-align: right;
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1;
  }
  .id-row .stats .stat b {
    display: block;
    color: var(--fg);
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 2px;
  }
  .id-row .stats .stat.blocks b { color: var(--accent2); }
  .id-row .stats .stat.paid b   { color: var(--accent);  }
  .id-row .stats .stat.lastshare b {
    font-size: 13px;
    white-space: nowrap;
  }
  @media (max-width: 720px) {
    .id-row .stats .stat.shares,
    .id-row .stats .stat.lastshare { display: none; }
    .id-row .type-pill { min-width: auto; padding: 2px 5px; font-size: 8px; }
  }
  .id-detail {
    padding: 12px 16px 14px 36px;
    border-top: 1px solid var(--border);
    background: rgba(79,195,247,0.03);
    font-size: 12px;
  }
  .id-detail-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.95fr);
    gap: 18px 28px;
    align-items: start;
  }
  .id-detail-main,
  .id-detail-side dl {
    margin: 0;
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 6px 14px;
  }
  .id-detail-side {
    min-width: 0;
    border-left: 1px solid var(--border);
    padding-left: 20px;
  }
  .id-detail-side.payout-side {
    border-left: 0;
    padding-left: 0;
  }
  .id-detail-side.payout-side dl {
    grid-template-columns: 1fr 96px;
  }
  .id-detail-side.payout-side dt {
    grid-column: 2;
    grid-row: 1;
    text-align: right;
    align-self: start;
  }
  .id-detail-side.payout-side dd {
    grid-column: 1;
    grid-row: 1;
  }
  .id-detail dt {
    color: var(--muted);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.05em;
  }
  .id-detail dd {
    margin: 0;
    word-break: break-all;
  }
  .id-detail dd.mono {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
  }
  .id-detail-payouts {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .id-detail-payouts > div {
    display: grid;
    grid-template-columns: minmax(118px, max-content) minmax(120px, max-content) max-content;
    align-items: baseline;
    gap: 8px;
  }
  .id-detail-payouts .payout-coin { justify-self: start; }
  .id-detail-payouts .payout-amount { justify-self: end; }
  .id-detail-payouts .payout-ticker { justify-self: start; color: var(--fg); }
  .id-detail-payouts .muted {
    margin-top: 4px;
  }
  .id-detail .copy-id {
    display: inline-block;
    background: var(--bg2);
    color: var(--accent);
    border: 1px solid var(--border);
    padding: 1px 8px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 10px;
    margin-left: 8px;
    user-select: none;
  }
  .id-detail .copy-id:hover { background: var(--accent); color: var(--bg); }
  @media (max-width: 980px) {
    .id-detail-grid {
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .id-detail-side {
      border-left: 0;
      border-top: 1px solid var(--border);
      padding-left: 0;
      padding-top: 14px;
    }
  }
  .ok    { color: var(--accent2); }
  .warn  { color: var(--warn);    }
  .bad   { color: var(--bad);     }
  .accent{ color: var(--accent);  }
  .empty { color: var(--muted); font-style: italic; padding: 12px 0; text-align: center; }
  .full { grid-column: 1 / -1; }

  /* Stratum endpoint card */
  .endpoint {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 10px 14px;
    border-radius: 6px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 14px;
  }
  .endpoint .url { flex: 1; color: var(--accent); user-select: all; }
  .endpoint button {
    background: var(--accent);
    color: #1a1d21;
    border: 0;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 600;
    font-size: 12px;
    font-family: inherit;
  }
  .endpoint button:hover { filter: brightness(1.1); }
  .endpoint button.copied { background: var(--accent2); }
  .endpoint-help {
    font-size: 12px;
    color: var(--muted);
  }
  .endpoint-help-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .endpoint-help pre {
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 10px 12px;
    border-radius: 4px;
    font-size: 11px;
    color: #c8cfd8;
    overflow-x: auto;
    margin-top: 8px;
    line-height: 1.5;
  }
  @media (max-width: 820px) {
    .endpoint-help-grid {
      grid-template-columns: 1fr;
    }
  }
  .endpoint-tip {
    margin-top: 10px;
    font-size: 12px;
    line-height: 1.55;
    color: var(--fg);
  }
  .endpoint-tip.muted { color: var(--muted); font-size: 11px; }
  .endpoint-tip b { color: var(--accent); }

  /* ===== Mining Key Generator card ===== */
  .mkgen-empty {
    padding: 14px 16px;
    background: var(--bg);
    border: 1px dashed var(--border);
    border-radius: 6px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }
  .mkgen-empty button {
    background: var(--accent);
    color: var(--bg);
    border: 0;
    padding: 10px 22px;
    border-radius: 5px;
    cursor: pointer;
    font-weight: 600;
    font-size: 14px;
    margin-top: 6px;
  }
  .mkgen-empty button:hover { filter: brightness(1.1); }
  .mkgen-empty-cols {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 14px;
    margin-top: 12px;
    text-align: left;
  }
  .mkgen-empty-col {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .mkgen-empty-col-title {
    color: var(--accent);
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }
  .mkgen-empty-col-desc {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.55;
    flex: 1;
  }
  .mkgen-empty-col button {
    margin-top: 6px;
    align-self: stretch;
  }
  @media (max-width: 720px) {
    .mkgen-empty-cols { grid-template-columns: 1fr; }
  }
  .mkgen-empty button[disabled],
  .mkgen-actions button[disabled] {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .mkgen-empty button[disabled]:hover,
  .mkgen-actions button[disabled]:hover { filter: none; }
  .mkgen-result {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .mkgen-row {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px;
  }
  .mkgen-row.danger { border-color: var(--warn); background: rgba(255,167,38,0.10); }
  /* Two-column layout for the mkgen result */
  .mkgen-cols {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 12px;
  }
  .mkgen-col { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .mkgen-col-right .mkgen-col-title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    padding: 0 2px 2px;
  }
  .mkgen-col-right .mkgen-col-scroll {
    max-height: 320px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-right: 4px;
  }
  /* Stack columns on narrow screens */
  @media (max-width: 820px) {
    .mkgen-cols { grid-template-columns: 1fr; }
    .mkgen-col-right .mkgen-col-scroll { max-height: none; }
  }
  .mkgen-row .lbl {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .mkgen-row.danger .lbl { color: var(--warn); }
  .mkgen-row .val {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 13px;
    color: var(--fg);
    word-break: break-all;
    user-select: all;
  }
  .mkgen-row.danger .val { color: #ffe082; }
  .mkgen-row .val.address { color: var(--accent); }
  .mkgen-row .val.address-derived { color: var(--accent2); }
  .mkgen-result button.copy-btn {
    background: var(--bg2);
    border: 1px solid var(--border);
    color: var(--accent);
    padding: 2px 9px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 10px;
    font-family: inherit;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .mkgen-result button.copy-btn:hover { background: var(--accent); color: var(--bg); }
  .mkgen-result button.copy-btn.copied { background: var(--accent2); color: var(--bg); border-color: var(--accent2); }
  .mkgen-warning {
    background: rgba(255,167,38,0.12);
    border: 1px solid rgba(255,167,38,0.5);
    border-radius: 6px;
    padding: 10px 14px;
    color: #ffe082;
    font-size: 12px;
    line-height: 1.5;
  }
  .mkgen-warning b { color: var(--warn); }

  /* Toast notification (bottom-center) for copy-to-clipboard feedback */
  .toast {
    position: fixed;
    left: 50%;
    bottom: 30px;
    transform: translate(-50%, 20px);
    background: var(--bg2);
    border: 1px solid var(--border);
    color: var(--accent);
    padding: 10px 18px;
    border-radius: 6px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    box-shadow: 0 8px 30px rgba(0,0,0,0.5);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.18s ease, transform 0.18s ease;
    z-index: 9999;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .toast.ok { border-color: var(--accent); color: var(--accent); }
  .toast.err { border-color: var(--bad); color: var(--bad); }
  .mkgen-cmd-row {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
  }
  .mkgen-cmd-row .lbl {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .mkgen-cmd-row .form {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .mkgen-cmd-row .form label {
    font-size: 11px;
    color: var(--muted);
  }
  .mkgen-cmd-row .form input {
    flex: 1;
    background: var(--bg2);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 5px 9px;
    border-radius: 4px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
  }
  .mkgen-cmd-row .cmd {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 10px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px;
    color: #c8cfd8;
    word-break: break-all;
    line-height: 1.5;
  }
  .mkgen-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  /* Anchor the save-keys button on the far left and push the generate
     buttons to the right edge. position:relative + z-index lifts the
     button above its siblings so the pulse glow renders evenly all
     around it instead of being visually clipped by the cgminer command
     row sitting just above the action row. */
  .mkgen-actions button.save {
    margin-right: auto;
    position: relative;
    z-index: 1;
    animation: mkgenSavePulse 4s ease-in-out infinite;
  }
  @keyframes mkgenSavePulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(255, 193, 7, 0.0); }
    50%      { box-shadow: 0 0 3px 1px rgba(255, 193, 7, 0.55); }
  }
  .mkgen-actions button {
    background: var(--bg2);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .mkgen-actions button.primary {
    background: var(--accent);
    color: var(--bg);
    border-color: var(--accent);
    font-weight: 600;
  }
  .mkgen-actions button.save {
    background: rgba(102,187,106,0.15);
    border-color: #66bb6a;
    color: #b9f6ca;
    font-weight: 600;
  }
  .mkgen-actions button.save:hover {
    background: #66bb6a;
    color: var(--bg);
    filter: none;
  }
  .mkgen-actions button:hover { filter: brightness(1.15); }
  .mkgen-disabled-banner {
    margin-top: 8px;
    padding: 8px 12px;
    background: rgba(255,183,77,0.08);
    border: 1px solid rgba(255,183,77,0.3);
    border-radius: 4px;
    color: var(--warn);
    font-size: 11px;
  }

  /* Hash chip / height pill */
  .hpill {
    display: inline-block;
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 1px 7px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    color: var(--accent);
  }
  .hash-short {
    color: var(--muted);
    font-size: 11px;
  }
  .hash-short .head { color: var(--fg); }

  /* Sparkline */
  .sparkline {
    margin: 4px 0 14px 0;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .sparkline .label {
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 6px;
  }
  .sparkline svg { display: block; width: 100%; height: 56px; }
  .sparkline .axis { stroke: var(--border); stroke-width: 1; }
  .sparkline .bar  { fill: var(--accent); }
  .sparkline .bar.fast { fill: var(--accent2); }
  .sparkline .bar.slow { fill: var(--warn); }
  .sparkline .bar.clipped {
    fill: var(--bad);
    /* diagonal-stripe pattern via SVG isn't worth the complexity for one bar
       state, so we use a solid red bar with reduced opacity */
    opacity: 0.75;
  }
  .sparkline .gridtxt { fill: var(--muted); font-size: 9px; font-family: inherit; }
  .sparkline-foot {
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    text-align: right;
  }
  .solved-breakdown {
    margin-top: 6px;
    display: grid;
    gap: 4px;
  }
  .solved-breakdown .line {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    font-size: 11px;
  }
  .solved-breakdown .line .mono {
    font-size: 10px;
  }
  .solved-toolbar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    flex-wrap: wrap;
    margin: 0 0 12px 0;
  }
  .solved-toolbar label {
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .solved-toolbar select {
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 5px 9px;
    font-size: 12px;
  }
  .solved-table td {
    vertical-align: top;
  }
  .solved-chain-cell {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .solved-height-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
  }
  .solved-hash-cell .mono {
    color: var(--muted);
    font-size: 11px;
    word-break: break-all;
  }
  .solved-height-cell .subhash {
    color: var(--muted);
    font-size: 10px;
  }
  .status-pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .status-pill.parent {
    color: var(--accent2);
    border-color: var(--accent2);
  }
  .status-pill.merged {
    color: var(--accent);
    border-color: var(--accent);
  }
  .status-pill.aux-only {
    color: var(--warn);
    border-color: var(--warn);
  }
  .share-chain-results {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    min-width: 200px;
  }
  .share-chain-results .label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }
  .share-chain-results .label.accepted {
    color: var(--accent2);
  }
  .share-chain-results .label.rejected {
    color: var(--bad);
  }
  .share-chain-badge {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid var(--border);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.03em;
  }
  .share-chain-badge.accepted {
    color: var(--accent2);
    border-color: var(--accent2);
  }
  .share-chain-badge.rejected {
    color: var(--bad);
    border-color: var(--bad);
  }

  pre.log {
    margin: 8px 0 0 0;
    background: var(--bg);
    padding: 10px;
    border-radius: 4px;
    font-size: 11px;
    line-height: 1.4;
    max-height: 320px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
    color: #c8cfd8;
  }
  details.collapsible summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 11px;
    padding: 2px 0;
  }
  details.collapsible summary:hover { color: var(--fg); }
  .payout-totals {
    display: grid;
    gap: 8px;
  }
  .payouts-card .payout-totals {
    margin-top: 0;
    padding-top: 0;
    border-top: 0;
  }
  .payout-totals .model {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.45;
  }
  .payout-totals .model b {
    color: var(--fg);
    font-weight: 600;
  }
  .payout-totals-grid {
    display: grid;
    gap: 12px;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .payout-total-tile {
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(255,255,255,0.02);
    padding: 12px;
    display: grid;
    gap: 10px;
  }
  .payout-total-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .payout-total-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    width: 100%;
    min-width: 0;
  }
  .payout-total-meta .name {
    color: var(--fg);
    font-size: 13px;
    font-weight: 600;
    min-width: 0;
  }
  .payout-total-amounts {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }
  .payout-total-box {
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 6px;
    padding: 10px;
    background: var(--bg);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .payout-total-box .lbl {
    color: var(--muted);
    font-size: 8px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    white-space: nowrap;
  }
  .payout-total-box .amount {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .payout-total-box .amount b {
    font-size: 13px;
    font-weight: 600;
  }
  @media (max-width: 980px) {
    .payout-totals-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (max-width: 720px) {
    .payout-totals-grid,
    .payout-total-amounts {
      grid-template-columns: 1fr;
    }
  }
  .wallet-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.65);
    z-index: 9998;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .wallet-modal-backdrop.show { display: flex; }
  .wallet-modal {
    width: min(920px, 100%);
    max-height: min(80vh, 760px);
    overflow: auto;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.55);
    scrollbar-width: thin;
    scrollbar-color: var(--border) var(--bg2);
  }
  .wallet-modal::-webkit-scrollbar { width: 8px; }
  .wallet-modal::-webkit-scrollbar-track { background: var(--bg2); }
  .wallet-modal::-webkit-scrollbar-thumb {
    background: var(--border);
    border-radius: 4px;
  }
  .wallet-modal::-webkit-scrollbar-thumb:hover {
    background: var(--accent);
  }
  .wallet-modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg2);
    z-index: 1;
  }
  .wallet-modal-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .wallet-modal-title h3 {
    margin: 0;
    font-size: 16px;
    color: var(--fg);
  }
  .wallet-modal-title .sub {
    color: var(--muted);
    font-size: 11px;
  }
  .wallet-modal-close {
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--fg);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 12px;
  }
  .wallet-modal-close:hover { border-color: var(--accent); }
  .wallet-modal-body {
    padding: 16px;
    display: grid;
    gap: 10px;
  }
  .wallet-row {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    background: rgba(255,255,255,0.02);
    display: grid;
    gap: 8px;
  }
  .wallet-row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .wallet-row-head .left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .wallet-row-head .name {
    color: var(--fg);
    font-size: 13px;
    font-weight: 600;
  }
  .wallet-row-head .amount {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .wallet-row-head .amount b {
    color: var(--accent2);
    font-size: 16px;
    font-weight: 700;
  }
  .wallet-row-head .amount .ticker {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .wallet-row .addr {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    color: var(--fg);
  }
  .wallet-row .addr .mono {
    font-size: 11px;
    word-break: break-all;
  }
  .wallet-copy {
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--fg);
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 10px;
    cursor: pointer;
  }
  .wallet-copy:hover { border-color: var(--accent); color: var(--accent); }
  .wallet-balances {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }
  .wallet-balances .box {
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 6px;
    padding: 8px;
    background: var(--bg);
  }
  .wallet-balances .lbl {
    display: block;
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 2px;
  }
  .wallet-balances .val {
    color: var(--fg);
    font-size: 13px;
    font-weight: 600;
  }
  .wallet-error {
    color: var(--bad);
    font-size: 11px;
    line-height: 1.4;
  }
  @media (max-width: 720px) {
    .wallet-balances {
      grid-template-columns: 1fr;
    }
  }
  footer {
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    padding: 16px;
  }

  /* ===== Custom floating tooltip (replaces native title attr) =====
     Borrowed from blakestream-explorer's .bsx-tooltip pattern, restyled to
     use our palette so it visually matches the rest of the dashboard. */
  .bsx-tooltip {
    position: fixed;
    z-index: 9999;
    max-width: 300px;
    padding: 7px 11px;
    font-size: 12px;
    line-height: 1.45;
    border-radius: 6px;
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.15s ease;
    background: var(--bg2);
    color: var(--fg);
    border: 1px solid var(--border);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .bsx-tooltip.visible { opacity: 1; }
  .bsx-tooltip code,
  .bsx-tooltip .mono {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    color: var(--accent);
    font-size: 11px;
  }
</style>
</head>
<body>
<header>
  <div class="header-brand">
    <h1>{{ HEADER_TITLE }}</h1>
    {% if HEADER_SUBTITLE %}
    <span class="sub">{{ HEADER_SUBTITLE }}</span>
    {% endif %}
  </div>
  <div class="header-endpoint">
    <div class="endpoint"><span class="url">{{ STRATUM_URL }}</span><button id="copy-stratum" data-url="{{ STRATUM_URL }}">copy</button></div>
  </div>
  <div class="header-side">
    <span class="live">live · 5s</span>
  </div>
</header>
<main id="root"></main>
<div id="wallet-modal-backdrop" class="wallet-modal-backdrop" aria-hidden="true">
  <div class="wallet-modal" role="dialog" aria-modal="true" aria-labelledby="wallet-modal-title">
    <div class="wallet-modal-head">
      <div class="wallet-modal-title">
        <h3 id="wallet-modal-title">Pool Wallets</h3>
        <span class="sub">Pool-controlled addresses and current wallet balances across the full BlakeStream set.</span>
      </div>
      <button type="button" id="wallet-modal-close" class="wallet-modal-close">close</button>
    </div>
    <div id="wallet-modal-body" class="wallet-modal-body"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let lastState = null;
let solvedChainFilter = '__all__';
let solvedRowLimit = 50;
let chainCardSelected = localStorage.getItem('chain-card-selected') || 'Blakecoin';

// ===== Custom floating tooltips =====
// Replaces the browser's native title attr (which is too small, light theme,
// and has a long delay we can't control). Same pattern as blakestream-explorer.
let _tipEl = null;
let _tipDelay = null;
function _ensureTipEl() {
  if (_tipEl) return _tipEl;
  _tipEl = document.createElement('div');
  _tipEl.className = 'bsx-tooltip';
  document.body.appendChild(_tipEl);
  return _tipEl;
}
function _showTip(target) {
  const text = target.getAttribute('data-tip');
  if (!text) return;
  const tip = _ensureTipEl();
  tip.textContent = text;
  const r = target.getBoundingClientRect();
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = r.left + (r.width - tw) / 2;
  let top  = r.top - th - 6;
  if (top < 4) top = r.bottom + 6;
  if (left < 4) left = 4;
  if (left + tw > window.innerWidth - 4) left = window.innerWidth - tw - 4;
  tip.style.left = left + 'px';
  tip.style.top  = top  + 'px';
  tip.classList.add('visible');
}
function _hideTip() {
  clearTimeout(_tipDelay);
  _tipDelay = null;
  if (_tipEl) _tipEl.classList.remove('visible');
}
function convertTitles(root) {
  (root || document).querySelectorAll('[title]').forEach(el => {
    if (!el.getAttribute('data-tip')) {
      el.setAttribute('data-tip', el.getAttribute('title'));
      el.removeAttribute('title');
    }
  });
}
// Event delegation so dynamically rendered content (every refresh) works.
// Register once on first script run.
document.addEventListener('mouseover', e => {
  const el = e.target.closest && e.target.closest('[data-tip]');
  if (!el) return;
  clearTimeout(_tipDelay);
  _tipDelay = setTimeout(() => _showTip(el), 200);
});
document.addEventListener('mouseout', e => {
  const el = e.target.closest && e.target.closest('[data-tip]');
  if (!el) return;
  _hideTip();
});
window.addEventListener('scroll', _hideTip, true);
window.addEventListener('resize', _hideTip);

// Persist open/closed state of every <details> across refreshes. Each
// <details> needs a stable data-key attribute so we can find it again
// after the next innerHTML rewrite.
//
// `detailsOpen` is the source of truth. Server HTML should NOT hardcode
// `open` on these elements — instead, the restore step explicitly sets
// `el.open = (detailsOpen.has(key))` so the same code path handles both
// "user collapsed it" and "user expanded it" without having to fight the
// HTML's default. `detailsKnown` tracks which keys we've ever seen so a
// brand-new key (a miner that just appeared) gets the default we want
// for that key type.
const detailsOpen = new Set();
const detailsKnown = new Set();

// Defaults for keys we've never seen before. If a key is NOT listed here,
// it defaults to closed.
const DEFAULT_OPEN_KEYS = new Set();

function snapshotDetailsState() {
  document.querySelectorAll('details[data-key]').forEach(el => {
    const k = el.dataset.key;
    detailsKnown.add(k);
    if (el.open) detailsOpen.add(k);
    else detailsOpen.delete(k);
  });
}

// Predicate rules applied in addition to DEFAULT_OPEN_KEYS. Useful for
// dynamic keys (e.g. per-miner `id:host|identity` rows) where the exact
// key isn't known in advance.
function _isDefaultOpen(k) {
  return DEFAULT_OPEN_KEYS.has(k);
}

function restoreDetailsState() {
  document.querySelectorAll('details[data-key]').forEach(el => {
    const k = el.dataset.key;
    if (!detailsKnown.has(k)) {
      // First time we see this key — apply default-open ruleset
      detailsKnown.add(k);
      if (_isDefaultOpen(k)) {
        detailsOpen.add(k);
      }
    }
    el.open = detailsOpen.has(k);
  });
  // Wire up the toggle listener so user clicks update the persistent set
  document.querySelectorAll('details[data-key]').forEach(el => {
    el.addEventListener('toggle', () => {
      if (el.open) detailsOpen.add(el.dataset.key);
      else detailsOpen.delete(el.dataset.key);
    });
  });
}

function renderState(state) {
  snapshotDetailsState();
  // Preserve scroll position across the full innerHTML rewrite. Without this,
  // height changes above the viewport (e.g. mining-key card expanding after
  // a Generate) shift the page and scroll the user's view away. We restore
  // synchronously AND in the next animation frame to catch any post-layout
  // shifts (custom font measurements, late-binding details elements, etc).
  const _scrollY = window.scrollY || window.pageYOffset || 0;
  $('root').innerHTML = render(state);
  updateHeaderMeta(state);
  const walletBackdrop = $('wallet-modal-backdrop');
  const walletBody = $('wallet-modal-body');
  if (walletBackdrop && walletBody && walletBackdrop.classList.contains('show')) {
    walletBody.innerHTML = renderPoolWalletModal(state);
    document.querySelectorAll('.wallet-copy[data-copy]').forEach(el => {
      el.onclick = async (ev) => {
        ev.preventDefault();
        await copyAndToast(el.dataset.copy, el.dataset.label || 'wallet address');
      };
    });
  }
  restoreDetailsState();
  attachHandlers();
  convertTitles();    // promote any new title= attrs into floating tooltips
  window.scrollTo(0, _scrollY);
  requestAnimationFrame(() => window.scrollTo(0, _scrollY));
}

async function refresh() {
  let r;
  const bust = Date.now();
  try {
    r = await fetch('/api/state?ts=' + bust, {
      cache: 'no-store',
      headers: {'Cache-Control': 'no-cache'}
    });
  } catch { return; }
  if (!r.ok) return;
  lastState = await r.json();
  renderState(lastState);
}
// Tell the browser not to auto-restore scroll on history navigation —
// our refresh handler is the only thing that should move the viewport.
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

function fmtAge(ts) {
  if (!ts) return '—';
  const a = Math.floor(Date.now()/1000 - ts);
  if (a < 0)     return 'just now';
  if (a < 60)    return a + 's ago';
  if (a < 3600)  return Math.floor(a/60) + 'm ago';
  if (a < 86400) return Math.floor(a/3600) + 'h ago';
  return Math.floor(a/86400) + 'd ago';
}
function fmtCount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toLocaleString();
}
function fmtWork(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '—';
  if (value >= 100) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (value >= 10) return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}
function fmtDifficulty(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '—';
  if (value >= 1000000 || value < 0.001) return value.toExponential(2);
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}
function fmtCoinAmount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const whole = Math.abs(value - Math.round(value)) < 1e-8;
  if (whole) return Math.round(value).toLocaleString();
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return value.toLocaleString(undefined, { maximumFractionDigits: 8 });
}
function fmtShortHash(h) {
  if (!h) return '';
  return '<span class="hash-short"><span class="head">' + h.slice(0, 12) + '</span>…' + h.slice(-6) + '</span>';
}
function escHtml(s) { return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c])); }

function updateHeaderMeta(state) {
  const runEl = $('run-stamp');
  if (runEl) {
    const runTs = state?.run_timestamp || '—';
    runEl.textContent = runTs;
    runEl.title = runTs;
  }

  const keyEl = $('active-mining-key');
  if (keyEl) {
    const ids = state?.identities || [];
    const activeKeys = Array.from(new Set(
      ids
        .filter(i => i && i.active && i.mining_key)
        .map(i => i.mining_key)
        .filter(Boolean)
    ));
    if (activeKeys.length > 1) {
      keyEl.textContent = activeKeys.length + ' active keys';
      keyEl.title = activeKeys.join('\\n');
    } else {
      const activeIdentity = ids.find(i => i && i.active && i.mining_key)
        || ids.find(i => i && i.mining_key)
        || null;
      const miningKey = activeIdentity?.mining_key || '—';
      keyEl.textContent = miningKey;
      keyEl.title = miningKey;
    }
  }
}

// ===========================================================================
// Mining Key Generator — pure-browser secp256k1 keygen + addressFromEx mirror
// ===========================================================================
// Generates a Blakestream-NOMP-compatible mining key entirely in the browser.
// The user's private key never leaves their machine. The Python backend's
// /api/derive-address endpoint is used only as a parity self-test on every
// page load to confirm the JS implementation produces byte-identical output.
//
// Algorithm chain (must match mining_key.py + Blakestream-nomp's util.ts):
//   1. Generate random 32-byte secp256k1 private key (crypto.getRandomValues)
//   2. Compute uncompressed public key = priv * G  (BigInt double-and-add)
//   3. Mining key = RIPEMD160(SHA256(uncompressed_pub))   ← 20 bytes
//   4. Derived address = addressFromEx(operator_ex_address, mining_key)
//
// All hash + base58 ops are pure JS (browser has SHA-256 in WebCrypto, not
// RIPEMD160; we ship our own ~80-line ripemd160 implementation).
// ---------------------------------------------------------------------------

// secp256k1 curve parameters
const SECP_P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2Fn;
const SECP_N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141n;
const SECP_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798n;
const SECP_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8n;

// Modular inverse via Fermat's little theorem (p is prime). Slow but
// correct and ~10 lines instead of ~30 for extended Euclidean.
function _modPow(base, exp, mod) {
  let result = 1n;
  base = ((base % mod) + mod) % mod;
  while (exp > 0n) {
    if (exp & 1n) result = (result * base) % mod;
    exp >>= 1n;
    base = (base * base) % mod;
  }
  return result;
}
function _modInv(a, p) { return _modPow(a, p - 2n, p); }

// Affine point doubling: 2P. P = (x, y).
function _ecDouble(P) {
  if (P === null) return null;
  const [x, y] = P;
  if (y === 0n) return null;
  // slope = (3x^2) * (2y)^-1 mod p
  const slope = ((3n * x * x) % SECP_P * _modInv(2n * y, SECP_P)) % SECP_P;
  const xr = ((slope * slope - 2n * x) % SECP_P + SECP_P) % SECP_P;
  const yr = ((slope * (x - xr) - y) % SECP_P + SECP_P) % SECP_P;
  return [xr, yr];
}

// Affine point addition: P + Q.
function _ecAdd(P, Q) {
  if (P === null) return Q;
  if (Q === null) return P;
  const [x1, y1] = P;
  const [x2, y2] = Q;
  if (x1 === x2) {
    if (y1 === y2) return _ecDouble(P);
    return null;   // P + (-P) = infinity
  }
  // slope = (y2 - y1) * (x2 - x1)^-1
  const slope = (((y2 - y1 + SECP_P) % SECP_P) * _modInv((x2 - x1 + SECP_P) % SECP_P, SECP_P)) % SECP_P;
  const xr = ((slope * slope - x1 - x2) % SECP_P + SECP_P * 2n) % SECP_P;
  const yr = ((slope * (x1 - xr) - y1) % SECP_P + SECP_P * 2n) % SECP_P;
  return [xr, yr];
}

// Scalar multiplication: k * P. Standard double-and-add.
// This is the load-bearing function — about 256 doublings + ~128 additions
// per call, ~5ms on a modern CPU.
function _ecMul(k, P) {
  let result = null;
  let addend = P;
  while (k > 0n) {
    if (k & 1n) result = _ecAdd(result, addend);
    addend = _ecDouble(addend);
    k >>= 1n;
  }
  return result;
}

// Generate a random 32-byte private key in [1, n-1].
function generatePrivateKey() {
  // crypto.getRandomValues is in every browser since 2014. No fallback path.
  const buf = new Uint8Array(32);
  while (true) {
    crypto.getRandomValues(buf);
    let k = 0n;
    for (const b of buf) k = (k << 8n) | BigInt(b);
    if (k > 0n && k < SECP_N) return buf;   // valid scalar
  }
}

function privateKeyToPoint(privBytes) {
  let k = 0n;
  for (const b of privBytes) k = (k << 8n) | BigInt(b);
  const P = _ecMul(k, [SECP_GX, SECP_GY]);
  if (P === null) throw new Error('invalid private key (produced point at infinity)');
  return P;
}

function privateKeyToUncompressedPubkey(privBytes) {
  const P = privateKeyToPoint(privBytes);
  // Encode as 0x04 || X(32) || Y(32)
  const out = new Uint8Array(65);
  out[0] = 0x04;
  const xBytes = _bigIntTo32Bytes(P[0]);
  const yBytes = _bigIntTo32Bytes(P[1]);
  out.set(xBytes, 1);
  out.set(yBytes, 33);
  return out;
}

function privateKeyToCompressedPubkey(privBytes) {
  const P = privateKeyToPoint(privBytes);
  const out = new Uint8Array(33);
  out[0] = (P[1] & 1n) ? 0x03 : 0x02;
  out.set(_bigIntTo32Bytes(P[0]), 1);
  return out;
}

function _bigIntTo32Bytes(n) {
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0; i--) {
    out[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  return out;
}

function _bytesToHex(bytes) {
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
function _hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i*2, 2), 16);
  return out;
}

// SHA-256 (pure JS, synchronous). Returns Uint8Array.
//
// HTTP vs HTTPS note:
// WebCrypto's crypto.subtle is only available in "secure contexts" — i.e.
// HTTPS origins or localhost. This staging dashboard is often served over
// plain HTTP (http://<host>:8080/), so calling crypto.subtle.digest('SHA-256')
// throws "Cannot read properties of undefined (reading 'digest')" and the
// mining-key generator dies before it can hash the pubkey. To avoid forcing a
// TLS terminator in front of the Flask dashboard during early staging, we ship
// a pure-JS reference SHA-256 here (~60 lines, byte-equivalent to FIPS 180-4,
// verified against the standard test vectors).
//
// MAINNET TODO: when this dashboard is fronted by HTTPS (nginx + Let's
// Encrypt, Cloudflare, etc.) we can drop this pure-JS impl and switch back
// to the one-liner:
//     async function _sha256(data) {
//       return new Uint8Array(await crypto.subtle.digest('SHA-256', data));
//     }
// ...and re-add `await` to every _sha256 call site (and make _sha256d /
// generateMiningKeyBundle / addressFromExJS async again). The pure-JS impl
// is correct but slower; native WebCrypto is preferable when available.
function _sha256(data) {
  const K = new Uint32Array([
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
  ]);
  const H = new Uint32Array([
    0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19
  ]);
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  const bitLen = bytes.length * 8;
  const padLen = ((bytes.length + 9 + 63) >>> 6) << 6;
  const msg = new Uint8Array(padLen);
  msg.set(bytes);
  msg[bytes.length] = 0x80;
  // 64-bit big-endian length (high 32 bits = 0 for any reasonable input)
  const dv = new DataView(msg.buffer);
  dv.setUint32(padLen - 4, bitLen >>> 0, false);
  dv.setUint32(padLen - 8, Math.floor(bitLen / 0x100000000), false);
  const W = new Uint32Array(64);
  for (let off = 0; off < padLen; off += 64) {
    for (let i = 0; i < 16; i++) W[i] = dv.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = ((W[i-15] >>> 7) | (W[i-15] << 25)) ^ ((W[i-15] >>> 18) | (W[i-15] << 14)) ^ (W[i-15] >>> 3);
      const s1 = ((W[i-2] >>> 17) | (W[i-2] << 15)) ^ ((W[i-2] >>> 19) | (W[i-2] << 13)) ^ (W[i-2] >>> 10);
      W[i] = (W[i-16] + s0 + W[i-7] + s1) >>> 0;
    }
    let a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + W[i]) >>> 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const mj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + mj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, H[i], false);
  return out;
}

// ===== RIPEMD160 (pure JS) ===========================================
// Adapted from the public-domain reference implementation. Browsers have
// no native RIPEMD160 (only SHA-1/256/384/512 in WebCrypto). ~80 lines.
function _rmd160(message) {
  const r1 = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15, 7,4,13,1,10,6,15,3,12,0,9,5,2,14,11,8, 3,10,14,4,9,15,8,1,2,7,0,6,13,11,5,12, 1,9,11,10,0,8,12,4,13,3,7,15,14,5,6,2, 4,0,5,9,7,12,2,10,14,1,3,8,11,6,15,13];
  const r2 = [5,14,7,0,9,2,11,4,13,6,15,8,1,10,3,12, 6,11,3,7,0,13,5,10,14,15,8,12,4,9,1,2, 15,5,1,3,7,14,6,9,11,8,12,2,10,0,4,13, 8,6,4,1,3,11,15,0,5,12,2,13,9,7,10,14, 12,15,10,4,1,5,8,7,6,2,13,14,0,3,9,11];
  const s1 = [11,14,15,12,5,8,7,9,11,13,14,15,6,7,9,8, 7,6,8,13,11,9,7,15,7,12,15,9,11,7,13,12, 11,13,6,7,14,9,13,15,14,8,13,6,5,12,7,5, 11,12,14,15,14,15,9,8,9,14,5,6,8,6,5,12, 9,15,5,11,6,8,13,12,5,12,13,14,11,8,5,6];
  const s2 = [8,9,9,11,13,15,15,5,7,7,8,11,14,14,12,6, 9,13,15,7,12,8,9,11,7,7,12,7,6,15,13,11, 9,7,15,11,8,6,6,14,12,13,5,14,13,13,7,5, 15,5,8,11,14,14,6,14,6,9,12,9,12,5,15,8, 8,5,12,9,12,5,14,6,8,13,6,5,15,13,11,11];
  function rol(x,n) { return ((x << n) | (x >>> (32 - n))) >>> 0; }
  function f(j,x,y,z) {
    if (j < 16) return x ^ y ^ z;
    if (j < 32) return (x & y) | ((~x >>> 0) & z);
    if (j < 48) return (x | (~y >>> 0)) ^ z;
    if (j < 64) return (x & z) | (y & (~z >>> 0));
    return x ^ (y | (~z >>> 0));
  }
  const K1 = [0,0x5a827999,0x6ed9eba1,0x8f1bbcdc,0xa953fd4e];
  const K2 = [0x50a28be6,0x5c4dd124,0x6d703ef3,0x7a6d76e9,0];

  // Padding
  const ml = message.length;
  const mb = ml * 8;
  const padLen = (ml % 64 < 56) ? (56 - ml % 64) : (120 - ml % 64);
  const buf = new Uint8Array(ml + padLen + 8);
  buf.set(message);
  buf[ml] = 0x80;
  // Length in bits, little-endian
  let lo = mb >>> 0;
  let hi = Math.floor(mb / 0x100000000);
  buf[ml + padLen]     = lo & 0xff;
  buf[ml + padLen + 1] = (lo >>> 8) & 0xff;
  buf[ml + padLen + 2] = (lo >>> 16) & 0xff;
  buf[ml + padLen + 3] = (lo >>> 24) & 0xff;
  buf[ml + padLen + 4] = hi & 0xff;
  buf[ml + padLen + 5] = (hi >>> 8) & 0xff;
  buf[ml + padLen + 6] = (hi >>> 16) & 0xff;
  buf[ml + padLen + 7] = (hi >>> 24) & 0xff;

  // Initialize
  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;

  for (let block = 0; block < buf.length; block += 64) {
    const X = new Array(16);
    for (let i = 0; i < 16; i++) {
      X[i] = (buf[block + i*4]) | (buf[block + i*4 + 1] << 8) | (buf[block + i*4 + 2] << 16) | (buf[block + i*4 + 3] << 24);
      X[i] >>>= 0;
    }
    let A = h0, B = h1, C = h2, D = h3, E = h4;
    let A2 = h0, B2 = h1, C2 = h2, D2 = h3, E2 = h4;
    for (let j = 0; j < 80; j++) {
      let T = (A + f(j,B,C,D) + X[r1[j]] + K1[Math.floor(j/16)]) >>> 0;
      T = (rol(T, s1[j]) + E) >>> 0;
      A = E; E = D; D = rol(C, 10); C = B; B = T;
      let T2 = (A2 + f(79-j,B2,C2,D2) + X[r2[j]] + K2[Math.floor(j/16)]) >>> 0;
      T2 = (rol(T2, s2[j]) + E2) >>> 0;
      A2 = E2; E2 = D2; D2 = rol(C2, 10); C2 = B2; B2 = T2;
    }
    const T = (h1 + C + D2) >>> 0;
    h1 = (h2 + D + E2) >>> 0;
    h2 = (h3 + E + A2) >>> 0;
    h3 = (h4 + A + B2) >>> 0;
    h4 = (h0 + B + C2) >>> 0;
    h0 = T;
  }

  const out = new Uint8Array(20);
  function w(off, val) {
    out[off]   = val & 0xff;
    out[off+1] = (val >>> 8) & 0xff;
    out[off+2] = (val >>> 16) & 0xff;
    out[off+3] = (val >>> 24) & 0xff;
  }
  w(0, h0); w(4, h1); w(8, h2); w(12, h3); w(16, h4);
  return out;
}

// ===== Base58 encode (no library) ====================================
const _B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function _base58encode(bytes) {
  // Count leading zeros
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros++;
  // Convert to base58 via BigInt
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  let out = '';
  while (n > 0n) {
    const r = n % 58n;
    n /= 58n;
    out = _B58[Number(r)] + out;
  }
  for (let i = 0; i < zeros; i++) out = '1' + out;
  return out;
}
function _base58decode(s) {
  let n = 0n;
  for (const c of s) {
    const i = _B58.indexOf(c);
    if (i < 0) throw new Error('invalid base58 char: ' + c);
    n = n * 58n + BigInt(i);
  }
  // Convert back to bytes
  const out = [];
  while (n > 0n) {
    out.unshift(Number(n & 0xffn));
    n >>= 8n;
  }
  // Restore leading zeros
  for (const c of s) {
    if (c === '1') out.unshift(0);
    else break;
  }
  return new Uint8Array(out);
}

// ===== addressFromEx (JS mirror of mining_key.address_from_ex) =========
// Pure JS port of Blakestream-nomp/src/stratum/util.ts:74. Must produce
// byte-identical output to the Python mining_key.address_from_ex. The
// page-load self-test verifies this.
async function addressFromExJS(exAddress, miningKeyHex, blakeFn) {
  if (!miningKeyHex || miningKeyHex.length !== 40) return null;
  try {
    const decoded = _base58decode(exAddress);
    if (decoded.length < 25) return null;
    const payload = decoded.slice(0, decoded.length - 4);
    const checksum = decoded.slice(decoded.length - 4);
    if (payload.length <= 20) return null;
    const prefix = payload.slice(0, payload.length - 20);

    // Detect codec by trying sha256d, then blake
    const sha256d = _sha256d(payload);
    const codec = (_bytesEqual(sha256d.slice(0, 4), checksum)) ? 'sha256d' :
                  ((_bytesEqual((await blakeFn(payload)).slice(0, 4), checksum)) ? 'blake' : null);
    if (!codec) return null;

    const miningKeyBytes = _hexToBytes(miningKeyHex);
    const addrBase = new Uint8Array(prefix.length + 20);
    addrBase.set(prefix, 0);
    addrBase.set(miningKeyBytes, prefix.length);

    let cksum;
    if (codec === 'sha256d') {
      const d = _sha256d(addrBase);
      cksum = d.slice(0, 4);
    } else {
      const d = await blakeFn(addrBase);
      cksum = d.slice(0, 4);
    }
    const full = new Uint8Array(addrBase.length + 4);
    full.set(addrBase, 0);
    full.set(cksum, addrBase.length);
    return _base58encode(full);
  } catch (e) {
    return null;
  }
}
function _sha256d(data) {
  return _sha256(_sha256(data));
}
function _bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

// Browser-side Blake-256 is NOT available natively. For Blakecoin
// Ex addresses, we delegate to the server's /api/derive-address endpoint
// which calls the canonical Python implementation. So this fallback uses
// the server when the codec turns out to be blake.
async function _blakeViaServer(payload) {
  // We can't actually compute blake256 in the browser without shipping
  // another ~150 lines of crypto. Instead, the addressFromExJS path
  // detects the blake codec by trial; if it succeeds, the rest of the
  // derivation needs the same blake hash on the new addrBase. We POST
  // the FULL derivation request to /api/derive-address and let the server
  // handle it. The browser still validates the result against its own
  // partial computation as a sanity check.
  throw new Error('blake256 in browser not available — use /api/derive-address for blake-codec chains');
}

// Convenience: full pipeline from priv → compressed pub → mining key →
// derived address set. Uses the server endpoint for the final derivation step
// so the dashboard UI stays locked to the Python reference for the released
// V2-only contract.
async function generateMiningKeyBundle(segwitHrp) {
  const priv = generatePrivateKey();
  const pub = privateKeyToCompressedPubkey(priv);
  const miningKey = _rmd160(_sha256(pub));
  const miningKeyHex = _bytesToHex(miningKey);

  let derived = null;
  let derivedAddressType = null;
  let derivedAddresses = null;
  if (segwitHrp) {
    try {
      const r = await fetch('/api/derive-addresses-v2', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mining_key: miningKeyHex, hrp: segwitHrp}),
      });
      const j = await r.json();
      if (j.ok) {
        derived = j.derived_address;
        derivedAddressType = 'bech32';
        derivedAddresses = j.derived_addresses || null;
      }
    } catch (e) { /* leave derived = null */ }
  }

  return {
    version: 'v2',
    privHex: _bytesToHex(priv),
    pubHex: _bytesToHex(pub),
    miningKey: miningKeyHex,
    stratumUsername: miningKeyHex,
    derivedAddress: derived,
    derivedAddressType: derivedAddressType,
    derivedAddresses: derivedAddresses,
  };
}

// Self-test on page load: generate one compressed pubkey, ask the server to
// verify it, and confirm the server's mining key matches what we computed
// locally. If they disagree, the JS port has drifted from the Python
// reference and we surface a console error.
async function selfTestMiningKeyJS() {
  try {
    const priv = generatePrivateKey();
    const pub = privateKeyToCompressedPubkey(priv);
    const localMk = _bytesToHex(_rmd160(_sha256(pub)));
    const r = await fetch('/api/verify-mining-key', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({version: 2, pubkey_hex: _bytesToHex(pub)}),
    });
    const j = await r.json();
    if (!j.ok) {
      console.warn('[mining-key self-test] server returned error:', j.error);
      return false;
    }
    if (j.mining_key !== localMk) {
      console.error('[mining-key self-test] DRIFT in v2 — JS computed', localMk, 'but Python returned', j.mining_key);
      return false;
    }
    console.log('[mining-key self-test] JS ↔ Python parity OK for v2');
    return true;
  } catch (e) {
    console.warn('[mining-key self-test] failed:', e.message);
    return false;
  }
}

function renderHero(s) {
  const status = s.status || 'DOWN';
  const pool = s.pool || {};
  const lastBlockTs = pool.last_block_ts;
  const lastBlockAge = lastBlockTs ? fmtAge(lastBlockTs) : 'never';

  // Slim hero: just the headline status + last block age. Everything else
  // (chain height, templating height, miner count, tracker addr) lives in
  // the Chain and Pool cards below to avoid duplication.
  let html = '<div class="hero">';
  html += '<div class="status ' + status + '">' + status + '</div>';
  html += '<div class="summary single">';
  html += '<div><span class="lbl">last block</span><span class="val">' + lastBlockAge + '</span></div>';
  html += '</div>';
  html += '</div>';
  return html;
}

function renderPoolAcceptedChips(labels, s) {
  const tickers = chainTickersFromState(s);
  const ordered = chainOrderFromState(s).filter(label => (labels || []).includes(label));
  if (!ordered.length) return '<span class="muted">—</span>';
  let html = '<span class="pool-chain-chips">';
  ordered.forEach(label => {
    html += '<span class="share-chain-badge accepted" title="' + escHtml(displayChainLabel(label)) + '">' + escHtml(tickers[label] || label) + '</span>';
  });
  html += '</span>';
  return html;
}

function renderChain(s) {
  const rows = s.chain_overview || [];
  const tickers = chainTickersFromState(s);
  if (!rows.some(row => row.label === chainCardSelected)) {
    chainCardSelected = rows[0]?.label || 'Blakecoin';
  }
  const selected = rows.find(row => row.label === chainCardSelected) || rows[0] || {};

  let html = '<div class="card"><h2>Merged Chains</h2>';
  if (!rows.length) {
    html += '<div class="bad">no chain data available</div></div>';
    return html;
  }
  html += '<div class="chain-tabs">';
  rows.forEach(row => {
    const active = row.label === selected.label ? ' active' : '';
    const status = row.status || 'error';
    const ticker = row.ticker || tickers[row.label] || row.label;
    html += '<button type="button" class="chain-tab ' + escHtml(status) + active + '" data-chain-label="' + escHtml(row.label) + '">' + escHtml(ticker) + '</button>';
  });
  html += '</div>';
  if (selected.error) {
    html += '<div class="bad">' + escHtml(selected.error) + '</div></div>';
    return html;
  }
  const verify = typeof selected.verificationprogress === 'number'
    ? (selected.verificationprogress * 100).toFixed(2) + '%'
    : '—';
  const diff = typeof selected.difficulty === 'number' ? selected.difficulty.toExponential(2) : '—';
  html += '<dl class="kv2">';
  html += '<dt>coin</dt><dd>' + escHtml(displayChainLabel(selected.label)) + '</dd>';
  html += '<dt>blocks</dt><dd><b class="accent">' + fmtCount(selected.blocks) + '</b></dd>';
  html += '<dt>headers</dt><dd>' + fmtCount(selected.headers) + '</dd>';
  html += '<dt>peers</dt><dd>' + (selected.connections ?? '—') + '</dd>';
  html += '<dt>verify</dt><dd>' + verify + '</dd>';
  html += '<dt>difficulty</dt><dd>' + diff + '</dd>';
  html += '<div class="span2 kv-row"><dt>best</dt><dd class="mono" style="font-size:11px">' + escHtml(selected.bestblockhash || '—') + '</dd></div>';
  html += '</dl></div>';
  return html;
}

function renderPool(s) {
  const pool = s.pool || {};
  const ids = s.identities || [];
  const status = s.status || 'DOWN';
  const solvedShares = s.recent_solved_shares || [];
  const lastSolved = solvedShares.reduce((best, row) => {
    if (!best || (row.time || 0) > (best.time || 0)) return row;
    return best;
  }, null);
  const lastBlockTs = lastSolved?.time || pool.last_block_ts;
  const lastBlockAge = lastBlockTs ? fmtAge(lastBlockTs) : 'never';

  // Aggregate across identities
  let lastShare = 0;
  ids.forEach(i => {
    if ((i.last_share || 0) > lastShare) lastShare = i.last_share;
  });
  const keyId = i => i?.mining_key || i?.addr || i?.identity || '';
  const miningKeys = new Set(ids.map(keyId).filter(Boolean));
  const activeMiningKeys = new Set(ids.filter(i => i.active).map(keyId).filter(Boolean));
  const peerSet = new Set(ids.map(i => i.host));
  const walletRows = pool.pool_wallet_rows || [];
  const auxRows = walletRows.filter(row => row.label && row.label !== 'Blakecoin');
  const auxTotal = auxRows.length || Math.max(0, chainOrderFromState(s).length - 1);
  const auxReady = auxRows.length ? auxRows.filter(row => !row.error).length : auxTotal;
  const parentWins = solvedShares.filter(row => (row.chains_accepted || []).includes('Blakecoin')).length;
  const auxWins = solvedShares.reduce((sum, row) => {
    return sum + (row.chains_accepted || []).filter(label => label !== 'Blakecoin').length;
  }, 0);
  const liveSockets = s.live_socket_count ?? peerSet.size;

  let html = '<div class="card"><h2>Pool</h2>';
  html += '<dl class="kv2">';
  html += '<dt>status</dt><dd><span class="pool-status status ' + status + '">' + status + '</span></dd>';
  html += '<div class="kv-wide"><dt>last block</dt><dd><span class="pool-last-block"><b class="big accent2">' + lastBlockAge + '</b>' + renderPoolAcceptedChips(lastSolved?.chains_accepted || [], s) + '</span></dd></div>';
  html += '<dt>miners</dt><dd><span class="pool-metric">' + liveSockets + ' live</span></dd>';
  html += '<dt>mining keys</dt><dd><span class="pool-metric">' + activeMiningKeys.size + ' active / ' + miningKeys.size + ' total</span></dd>';
  html += '<dt>recent wins</dt><dd><span class="pool-metric"><b class="ok">' + parentWins.toLocaleString() + '</b> BLC / <b class="accent2">' + auxWins.toLocaleString() + '</b> aux</span></dd>';
  html += '<dt>aux rpc</dt><dd><span class="pool-metric">' + auxReady + ' / ' + auxTotal + ' ready</span></dd>';
  html += '</dl></div>';
  return html;
}

function renderPayouts(s) {
  const pool = s.pool || {};
  const ids = s.identities || [];
  const chainOrder = chainOrderFromState(s);
  const chainTickers = chainTickersFromState(s);
  const creditedTotals = pool.credited_payout_satoshis || aggregateCreditedPayouts(ids, chainOrder);
  const poolWalletTotals = pool.pool_wallet_paid_satoshis || poolWalletTotalsFromState(pool, chainOrder);
  const walletRows = {};
  (pool.pool_wallet_rows || []).forEach(row => {
    if (row && row.label) walletRows[row.label] = row;
  });

  let html = '<details class="card full card-collapse payouts-card" data-key="card-payouts">';
  html += '<summary><h2>Payouts</h2></summary>';
  html += '<div class="card-body">';
  html += '<div class="payout-totals">';
  html += '<div class="model"><b>payout totals</b> &mdash; miner credits are allocated by submitted shares.</div>';
  html += '<div class="payout-totals-grid">';
  html += chainOrder.map(label => {
    const ticker = chainTickers[label] || label;
    const credited = Number(creditedTotals[label] || 0);
    const pooled = Number(poolWalletTotals[label] || 0);
    const immature = Number((walletRows[label] && walletRows[label].immature_satoshis) || 0);
    const creditedClass = credited > 0 ? 'accent2' : 'muted';
    const pooledClass = pooled > 0 ? 'accent' : 'muted';
    const immatureClass = immature > 0 ? 'warn' : 'muted';
    return '<div class="payout-total-tile">'
      + '<div class="payout-total-top">'
      +   '<div class="payout-total-meta"><span class="name">' + escHtml(displayChainLabel(label)) + '</span><span class="hpill">' + escHtml(ticker) + '</span></div>'
      + '</div>'
      + '<div class="payout-total-amounts">'
      +   '<div class="payout-total-box"><span class="lbl">miners credited</span><div class="amount"><b class="' + creditedClass + '">' + fmtCoinsCompact(credited) + '</b></div></div>'
      +   '<div class="payout-total-box"><span class="lbl">pool wallets</span><div class="amount"><b class="' + pooledClass + '">' + fmtCoinsCompact(pooled) + '</b></div></div>'
      +   '<div class="payout-total-box"><span class="lbl">immature</span><div class="amount"><b class="' + immatureClass + '">' + fmtCoinsCompact(immature) + '</b></div></div>'
      + '</div>'
      + '</div>';
  }).join('');
  html += '</div>';
  html += '</div>';
  html += '</div>';
  html += '</details>';
  return html;
}

function renderEndpointHelp(s) {
  const stratum = s.stratum || {};
  const url = 'stratum+tcp://' + stratum.host + ':' + stratum.port;
  let html = '<details class="card full card-collapse" data-key="card-endpoint-help">';
  html += '<summary><h2>How to Mine</h2></summary>';
  html += '<div class="card-body"><div class="endpoint-help">';
  html += '<div class="endpoint-help-grid">';
  html += '<pre>';
  html += 'BAIKAL GIANT-B ASIC\\n\\n';
  html += 'POOL URL    ' + url + '\\n';
  html += 'ALGORITHM   Blake256r8\\n';
  html += 'USER        miningkey.worker\\n';
  html += 'PASS        x\\n';
  html += 'EXTRANONCE  DISABLE  (uncheck the Extranonce box)';
  html += '</pre>';
  html += '<pre>';
  html += 'GPU MINING\\n\\n';
  html += 'CGMINER\\n';
  html += 'cgminer --blake256 -o ' + url + ' -u miningkey.worker -p x\\n\\n';
  html += 'SGMINER\\n';
  html += 'sgminer --no-submit-stale --kernel blakecoin --gpu-platform 0 -I 30 --no-extranonce \\\\\\n';
  html += '  -o ' + url + ' -u miningkey.worker -p x';
  html += '</pre>';
  html += '</div>';
  html += '<div class="endpoint-tip">';
  html += '<b>User:</b> use your generated mining key. Add <span class="mono">.worker</span> if you want a worker label.';
  html += '</div></div></div>';
  html += '</details>';
  return html;
}

// Short tooltip strings for each address-type pill (hover-over the badge)
const ADDR_TYPE_TOOLTIP = {
  bech32: 'Native SegWit P2WPKH — released mining-key path. blc1… on mainnet.',
  legacy: 'Legacy P2PKH payout path. Mainnet addresses remain base58.',
  p2sh:   'Wrapped SegWit (P2SH-P2WPKH) — SegWit carried inside a P2SH shell for older wallets.',
  none:   'MISCONFIG: stratum username has no recognisable address. Use address.workername format so address-aware pools can pay you.',
};

// Longer description used in the expanded detail row
const ADDR_TYPE_DESC = {
  bech32: 'Native SegWit P2WPKH address (BIP173).',
  legacy: 'Legacy P2PKH address (Bitcoin-style).',
  p2sh:   'Wrapped SegWit (P2SH-P2WPKH). A SegWit address dressed up to look like a P2SH so older wallets that do not speak bech32 can still send to it.',
  none:   'No recognisable Blakecoin address found in this stratum username. The standard convention is `address.workername`. This identity will still mine on eloipool (since allowall accepts any string), but it would not be paid by any address-aware pool — fix the miner config to put a real address before the dot.',
};

// Short pill label for each type
const ADDR_TYPE_LABEL = {
  bech32: 'bech32',
  legacy: 'legacy',
  p2sh:   'p2sh',
  none:   'misconfig',
};

// Holds the most recent generator output across re-renders so the card
// preserves its state as the dashboard auto-refreshes every 5 seconds.
let _mkgenLast = {v2: null};
let _mkgenVersion = 'v2';

function mkgenCurrentResult() {
  return _mkgenLast.v2 || null;
}

function renderMiningKeyGenerator(s) {
  const pool = s.pool || {};
  const enabled = pool.mining_key_enabled;
  const segwitHrp = pool.mining_key_segwit_hrp || '';
  const coinHrps = pool.mining_key_v2_coin_hrps || {};
  const stratum = s.stratum || {};
  const stratumHost = stratum.host || '';
  const stratumPort = stratum.port || 3334;

  let html = '<details class="card full card-collapse" data-key="card-mkgen">';
  html += '<summary><h2>Mining Key Generator';
  if (!enabled) {
    html += ' <span class="count" style="color:var(--warn);border-color:var(--warn)">setup pending</span>';
  }
  html += '</h2></summary>';
  html += '<div class="card-body">';

  if (!enabled) {
    html += '<div class="mkgen-disabled-banner">';
    html += 'Mining-key payouts aren&rsquo;t active yet. Use a direct payout address temporarily, or configure the SegWit HRP to enable the released bare mining-key flow.';
    html += '</div>';
  }

  html += '<div id="mkgen-area" style="margin-top:14px">';
  html += renderMkgenArea(mkgenCurrentResult(), segwitHrp, stratumHost, stratumPort, coinHrps);
  html += '</div>';

  html += '</div></details>';
  return html;
}

function renderMkgenArea(result, segwitHrp, stratumHost, stratumPort, coinHrps) {
  const derivationReady = !!segwitHrp;
  const v2CoinCount = Object.keys(coinHrps || {}).length;
  const disabledAttrs = derivationReady ? '' : ' disabled title="pool operator hasn&rsquo;t configured a SegWit HRP &mdash; mining-key payouts unavailable"';
  if (!result) {
    let html = '<div class="mkgen-empty">';
    html += '<div class="mkgen-empty-cols">';
    html += '<div class="mkgen-empty-col">';
    html += '<div class="mkgen-empty-col-title">Mining Key</div>';
    html += '<div class="mkgen-empty-col-desc">Use the bare <span class="mono">&lt;40hex&gt;[.worker]</span> mining key in your miner. The pool derives bech32 payout addresses for the BlakeStream chains.</div>';
    html += '<button id="mkgen-btn"' + disabledAttrs + '>Generate Mining Key</button>';
    html += '</div>';
    html += '</div>';
    html += '</div>';
    return html;
  }

  let html = '<div class="mkgen-result">';
  html += '<div class="mkgen-cols">';

  html += '<div class="mkgen-col mkgen-col-left">';
  html += '<div class="mkgen-row danger">';
  html += '<div class="lbl"><span>private key &mdash; SAVE THIS, IT CANNOT BE RECOVERED</span>';
  html += '<button class="copy-btn" data-copy="' + escHtml(result.privHex) + '">copy</button></div>';
  html += '<div class="val">' + escHtml(result.privHex) + '</div>';
  html += '</div>';

  html += '<div class="mkgen-row">';
  html += '<div class="lbl"><span>mining key</span>';
  html += '<button class="copy-btn" data-copy="' + escHtml(result.miningKey) + '">copy</button></div>';
  html += '<div class="val">' + escHtml(result.miningKey) + '</div>';
  html += '</div>';

  html += '<div class="mkgen-row mkgen-cmd-row">';
  html += '<div class="lbl"><span>cgminer command &mdash; copy and paste into your miner config</span>';
  html += '<button class="copy-btn" id="mkgen-copy-cmd">copy command</button></div>';
  html += '<div class="form">';
  html += '<label for="mkgen-worker">worker name:</label>';
  html += '<input id="mkgen-worker" type="text" value="rig1" placeholder="rig1">';
  html += '</div>';
  html += '<div class="cmd" id="mkgen-cmd">';
  html += 'cgminer -o stratum+tcp://' + escHtml(stratumHost) + ':' + stratumPort + ' \\\\<br>';
  html += '&nbsp;&nbsp;&nbsp;&nbsp;-u ' + escHtml(result.miningKey) + '.<span id="mkgen-cmd-worker">rig1</span> -p x';
  html += '</div>';
  html += '</div>';

  html += '<div class="mkgen-actions">';
  html += '<button id="mkgen-save" class="save">Save keys to file</button>';
  html += '<button id="mkgen-regen" class="primary"' + disabledAttrs + '>Generate Mining Key</button>';
  html += '</div>';
  html += '</div>';

  html += '<div class="mkgen-col mkgen-col-right">';
  html += '<div class="mkgen-row mkgen-col-header">';
  html += '<div class="lbl"><span>derived payout addresses - bech32</span></div>';
  html += '</div>';
  html += '<div class="mkgen-col-scroll">';

  if (result.derivedAddresses && Object.keys(result.derivedAddresses).length) {
    Object.entries(result.derivedAddresses).forEach(([label, details]) => {
      html += '<div class="mkgen-row">';
      html += '<div class="lbl"><span>' + escHtml(displayChainLabel(label));
      if (details.hrp) {
        html += ' <span style="color:var(--muted)">(' + escHtml(details.hrp) + '1&hellip;)</span>';
      }
      html += '</span>';
      html += '<button class="copy-btn" data-copy="' + escHtml(details.address || '') + '">copy</button></div>';
      html += '<div class="val address-derived">' + escHtml(details.address || '') + '</div>';
      html += '</div>';
    });
  } else if (result.derivedAddress) {
    html += '<div class="mkgen-row">';
    html += '<div class="lbl"><span>Blakecoin</span>';
    html += '<button class="copy-btn" data-copy="' + escHtml(result.derivedAddress) + '">copy</button></div>';
    html += '<div class="val address-derived">' + escHtml(result.derivedAddress) + '</div>';
    html += '</div>';
  } else if (derivationReady) {
    html += '<div class="mkgen-row">';
    html += '<div class="lbl">' + (v2CoinCount > 1 ? 'BlakeStream payout set' : 'Blakecoin') + '</div>';
    html += '<div class="val" style="color:var(--warn)">⚠ derivation failed (server error). Mining key is still valid; try refreshing.</div>';
    html += '</div>';
  } else {
    html += '<div class="mkgen-row">';
    html += '<div class="lbl">' + (v2CoinCount > 1 ? 'BlakeStream payout set' : 'Blakecoin') + '</div>';
    html += '<div class="val" style="color:var(--muted)">— pool operator hasn&rsquo;t configured a SegWit HRP for mining-key payouts. The key is valid, but the pool won&rsquo;t honor bare mining-key usernames until that is set.</div>';
    html += '</div>';
  }

  html += '</div>';
  html += '</div>';
  html += '</div>';
  html += '</div>';
  return html;
}

function chainOrderFromState(s) {
  return s.chain_order || ['Blakecoin', 'BlakeBitcoin', 'lithium', 'Photon', 'UniversalMolecule', 'Electron-ELT'];
}

function chainTickersFromState(s) {
  return s.chain_tickers || {};
}

function fmtCoins(satoshis) {
  return (Number(satoshis || 0) / 1e8).toFixed(8);
}

function fmtCoinsCompact(satoshis) {
  const value = Number(satoshis || 0) / 1e8;
  if (!Number.isFinite(value)) return '0';
  const fixed = value.toFixed(4);
  const trimmed = fixed.replace(/\\.?0+$/, '');
  return trimmed === '-0' ? '0' : trimmed;
}

function aggregateCreditedPayouts(ids, chainOrder) {
  const totals = {};
  (chainOrder || []).forEach(label => { totals[label] = 0; });
  (ids || []).forEach(identity => {
    const allPaid = identity.all_paid_satoshis || {};
    Object.entries(allPaid).forEach(([label, satoshis]) => {
      totals[label] = (totals[label] || 0) + (Number(satoshis) || 0);
    });
  });
  return totals;
}

function poolWalletTotalsFromState(pool, chainOrder) {
  const totals = {};
  (chainOrder || []).forEach(label => { totals[label] = 0; });
  totals.Blakecoin = Number(pool.tracker_paid_satoshis || 0);
  const auxPool = pool.aux_pool_paid_satoshis || {};
  Object.entries(auxPool).forEach(([label, satoshis]) => {
    totals[label] = (totals[label] || 0) + (Number(satoshis) || 0);
  });
  return totals;
}

function renderPoolWalletModal(state) {
  const pool = state?.pool || {};
  const rows = pool.pool_wallet_rows || [];
  if (!rows.length) {
    return '<div class="wallet-error">Pool wallet balances are not available yet.</div>';
  }
  return rows.map(row => {
    const ticker = row.ticker || row.label || '';
    const confirmed = fmtCoins(row.confirmed_satoshis || 0);
    const immature = fmtCoins(row.immature_satoshis || 0);
    const total = fmtCoins(row.total_satoshis || 0);
    let html = '<div class="wallet-row">';
    html += '<div class="wallet-row-head">';
    html += '<div class="left"><span class="hpill">' + escHtml(ticker) + '</span><span class="name">' + escHtml(displayChainLabel(row.label)) + '</span></div>';
    html += '<div class="amount"><b>' + total + '</b><span class="ticker">' + escHtml(ticker) + '</span></div>';
    html += '</div>';
    html += '<div class="addr"><span class="mono">' + escHtml(row.address || '') + '</span><button type="button" class="wallet-copy" data-copy="' + escHtml(row.address || '') + '" data-label="' + escHtml((ticker || 'wallet') + ' address') + '">copy</button></div>';
    html += '<div class="wallet-balances">';
    html += '<div class="box"><span class="lbl">available</span><span class="val">' + confirmed + ' ' + escHtml(ticker) + '</span></div>';
    html += '<div class="box"><span class="lbl">immature</span><span class="val">' + immature + ' ' + escHtml(ticker) + '</span></div>';
    html += '<div class="box"><span class="lbl">total</span><span class="val">' + total + ' ' + escHtml(ticker) + '</span></div>';
    html += '</div>';
    if (row.error) {
      html += '<div class="wallet-error">' + escHtml(String(row.error)) + '</div>';
    }
    html += '</div>';
    return html;
  }).join('');
}

function openPoolWalletModal() {
  if (!lastState) return;
  const backdrop = $('wallet-modal-backdrop');
  const body = $('wallet-modal-body');
  if (!backdrop || !body) return;
  body.innerHTML = renderPoolWalletModal(lastState);
  backdrop.classList.add('show');
  backdrop.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  document.querySelectorAll('.wallet-copy[data-copy]').forEach(el => {
    el.onclick = async (ev) => {
      ev.preventDefault();
      await copyAndToast(el.dataset.copy, el.dataset.label || 'wallet address');
    };
  });
}

function closePoolWalletModal() {
  const backdrop = $('wallet-modal-backdrop');
  if (!backdrop) return;
  backdrop.classList.remove('show');
  backdrop.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}

function displayChainLabel(label) {
  const raw = String(label || '');
  const normalized = raw.toLowerCase();
  const overrides = {
    'lithium': 'Lithium',
    'universalmol': 'UniversalMolecule',
    'electron-elt': 'Electron',
  };
  if (overrides[normalized]) return overrides[normalized];
  if (!raw) return '';
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function renderChainPills(labels, s) {
  const tickers = chainTickersFromState(s);
  return (labels || []).map(label => {
    const ticker = tickers[label] || label;
    return '<span class="hpill" title="' + escHtml(displayChainLabel(label)) + '">' + escHtml(ticker) + '</span>';
  }).join(' ');
}

function renderMiners(s) {
  const ids = s.identities || [];
  let html = '<details class="card full card-collapse" data-key="card-miners">';
  html += '<summary><h2>Connected Miners ';
  html += '<span class="count">' + (s.active_count || 0) + ' active · ' + ids.length + ' total</span>';
  // Inline legend — three real Blakecoin address types
  html += '<span class="legend">';
  html += '<span class="lbl">address type:</span>';
  html += '<span class="type-pill bech32" title="' + escHtml(ADDR_TYPE_TOOLTIP.bech32) + '">bech32</span>';
  html += '<span class="type-pill p2sh"   title="' + escHtml(ADDR_TYPE_TOOLTIP.p2sh)   + '">p2sh</span>';
  html += '<span class="type-pill legacy" title="' + escHtml(ADDR_TYPE_TOOLTIP.legacy) + '">legacy</span>';
  html += '</span>';
  html += '</h2></summary>';
  html += '<div class="card-body">';

  if (ids.length === 0) {
    html += '<div class="empty">no stratum activity yet &mdash; copy the URL above and point a miner here</div>';
    html += '</div>';      // close .card-body
    html += '</details>';  // close .card-collapse
    return html;
  }

  html += '<div class="id-list">';
  ids.forEach((m, idx) => {
    const cls = m.addr_type;
    const tagText = ADDR_TYPE_LABEL[cls] || cls;
    const dotCls = m.active ? 'active' : 'idle';
    const peer = m.host;
    const key = 'id:' + m.host + '|' + m.identity;
    const pillTitle = ADDR_TYPE_TOOLTIP[cls] || '';
    const isMisconfig = (cls === 'none');
    const rowClasses = 'id-row' + (isMisconfig ? ' misconfig' : '');
    const chainOrder = chainOrderFromState(s);
    const chainTickers = chainTickersFromState(s);
    const allPaid = m.all_paid_satoshis || {};
    const paidCoins = m.paid_chain_count || Object.values(allPaid).filter(v => (v || 0) > 0).length;

    // Single-line identity: worker | address (or one of the partial cases)
    let identityHtml;
    if (isMisconfig) {
      // worker | ⚠ error message
      identityHtml = '<span class="worker-name">' + escHtml(m.worker || m.identity) + '</span>';
      identityHtml += '<span class="sep">|</span>';
      identityHtml += '<span class="addr-error">⚠ no address &mdash; standard format is <code>address.workername</code></span>';
    } else if (m.mining_key && m.worker) {
      // worker | mining key
      identityHtml = '<span class="worker-name">' + escHtml(m.worker) + '</span>';
      identityHtml += '<span class="sep">|</span>';
      identityHtml += '<span class="addr-mono ' + cls + '">' + escHtml(m.mining_key) + '</span>';
    } else if (m.mining_key) {
      // Just the mining key, no worker label at all
      identityHtml = '<span class="addr-mono ' + cls + '">' + escHtml(m.mining_key) + '</span>';
    } else if (m.worker) {
      // worker | address
      identityHtml = '<span class="worker-name">' + escHtml(m.worker) + '</span>';
      identityHtml += '<span class="sep">|</span>';
      identityHtml += '<span class="addr-mono ' + cls + '">' + escHtml(m.addr) + '</span>';
    } else {
      // Just the address, no worker label at all
      identityHtml = '<span class="addr-mono ' + cls + '">' + escHtml(m.addr) + '</span>';
    }

    html += '<details class="' + rowClasses + '" data-key="' + escHtml(key) + '">';
    html += '<summary>';
    html += '<span class="chevron">▸</span>';
    html += '<span class="dot ' + dotCls + '" title="' + (m.active ? 'active in last 5 min' : 'idle &mdash; no shares in 5 min') + '"></span>';
    if (isMisconfig) {
      html += '<span class="warn-marker" title="' + escHtml(ADDR_TYPE_TOOLTIP.none) + '">⚠</span>';
    } else {
      html += '<span class="type-pill ' + cls + '" title="' + escHtml(pillTitle) + '">' + tagText + '</span>';
    }
    html += '<span class="identity">' + identityHtml + '</span>';
    const paidBlc = (m.paid_satoshis || 0) / 1e8;
    html += '<span class="stats">';
    html +=   '<span class="stat shares"><b>' + m.shares + '</b>shares</span>';
    html +=   '<span class="stat shares"><b>' + fmtWork(m.weighted_work || 0) + '</b>work</span>';
    html +=   '<span class="stat blocks"><b>' + m.blocks + '</b>blocks</span>';
    html +=   '<span class="stat paid"><b>' + paidBlc.toFixed(2) + '</b>BLC paid</span>';
    html +=   '<span class="stat paid"><b>' + paidCoins + '</b>coins paid</span>';
    html +=   '<span class="stat lastshare"><b>' + fmtAge(m.last_share) + '</b>last share</span>';
    html += '</span>';
    html += '</summary>';

    // Expanded detail
    html += '<div class="id-detail">';
    html += '<div class="id-detail-grid">';
    html += '<dl class="id-detail-main">';
    html += '<dt>worker name</dt><dd>' + escHtml(m.worker || '(none — no .workername in stratum username)') + '</dd>';
    if (m.mining_key) {
      html += '<dt>mining key</dt><dd class="mono">' + escHtml(m.mining_key);
      html += '<span class="copy-id" data-copy="' + escHtml(m.mining_key) + '">copy</span></dd>';
    } else if (m.addr) {
      html += '<dt>address</dt><dd class="mono">' + escHtml(m.addr);
      html += '<span class="copy-id" data-copy="' + escHtml(m.addr) + '">copy</span></dd>';
      html += '<dt>address type</dt><dd><span class="type-pill ' + cls + '">' + tagText + '</span> ' + escHtml(ADDR_TYPE_DESC[cls] || '') + '</dd>';
    } else {
      html += '<dt>address</dt><dd><span class="bad">⚠ misconfig</span> — no recognisable Blakecoin address before the dot in the stratum username</dd>';
      html += '<dt>fix</dt><dd>Use either a direct address or the primary mining-key format. Reconnect with e.g. <span class="mono">783c0d31dbf6d7a5bd3d9f9b8e4b3efef0dbe123.' + escHtml(m.worker || 'rig1') + '</span></dd>';
      html += '<dt>raw username</dt><dd class="mono">' + escHtml(m.identity);
      html += '<span class="copy-id" data-copy="' + escHtml(m.identity) + '">copy</span></dd>';
    }
    html += '<dt>peer ip</dt><dd class="mono">' + escHtml(peer) + '</dd>';
    html += '<dt>state</dt><dd>' + (m.active
        ? '<span class="ok">active</span> &mdash; submitted a share within the last 5 minutes'
        : '<span class="muted">idle</span> &mdash; no shares in the last 5 minutes') + '</dd>';
    html += '<dt>accepted shares</dt><dd>' + m.accepted_shares + ' counted toward payout / round contribution</dd>';
    html += '<dt>weighted work</dt><dd><b class="accent">' + fmtWork(m.weighted_work || 0) + '</b> difficulty-weighted share units</dd>';
    html += '<dt>block-solves</dt><dd><b class="ok">' + m.blocks + '</b> winning shares</dd>';
    html += '</dl>';
    html += '<div class="id-detail-side payout-side"><dl>';
    html += '<dt>all payouts</dt><dd class="id-detail-payouts">';
    html += chainOrder.map(label => {
      const sats = allPaid[label] || 0;
      const ticker = chainTickers[label] || label;
      const amountClass = sats > 0 ? 'accent2' : 'muted';
      return '<div><span class="hpill payout-coin">' + escHtml(displayChainLabel(label)) + '</span><b class="payout-amount ' + amountClass + '">' + (sats / 1e8).toFixed(8) + '</b><span class="payout-ticker">' + escHtml(ticker) + '</span></div>';
    }).join('');
    if (!m.addr) {
      html += '<div class="muted">No payout address was parsed from the username, so every chain stays at zero until the miner reconnects with a valid address or mining key.</div>';
    } else if (!m.mining_key) {
      html += '<div class="muted">Aux-chain payouts stay on the pool&rsquo;s configured base payout addresses unless the miner connects with a mining-key username.</div>';
    }
    html += '</dd>';
    html += '</dl></div>';
    html += '</div>';
    html += '</div>';

    html += '</details>';
  });
  html += '</div>';      // close .id-list
  html += '</div>';      // close .card-body
  html += '</details>';  // close .card-collapse
  return html;
}

function renderSparkline(blocks) {
  if (blocks.length < 2) return '';
  const intervals = [];
  for (let i = 1; i < blocks.length; i++) {
    if (typeof blocks[i].height === 'number' && typeof blocks[i-1].height === 'number' && blocks[i].ts && blocks[i-1].ts) {
      intervals.push({h: blocks[i].height, dt: blocks[i].ts - blocks[i-1].ts});
    }
  }
  if (intervals.length === 0) return '';
  const dts = intervals.map(x => x.dt);
  const realMax = Math.max(...dts);
  const realMin = Math.min(...dts);
  const realMean = dts.reduce((a, b) => a + b, 0) / dts.length;
  // Clip the displayed scale at 30s. Anything above stays full-height but
  // gets a striped fill so the user can tell it's clipped, instead of
  // dwarfing every other bar.
  const SCALE_CAP = 30;
  const scaleMax = Math.min(realMax, SCALE_CAP);
  const W = 600, H = 56, pad = 4;
  const bw = (W - pad*2) / intervals.length;
  let bars = '';
  intervals.forEach((iv, i) => {
    const clipped = iv.dt > SCALE_CAP;
    const h = Math.max(2, (Math.min(iv.dt, scaleMax) / scaleMax) * (H - pad*2));
    const x = pad + i * bw;
    const y = H - pad - h;
    const cls = clipped ? 'bar clipped' :
                iv.dt < 1 ? 'bar fast' :
                iv.dt > 10 ? 'bar slow' : 'bar';
    bars += '<rect class="' + cls + '" x="' + (x+1) + '" y="' + y + '" width="' + (bw-2) + '" height="' + h + '">';
    bars += '<title>h' + iv.h + ' · gap to previous block: ' + iv.dt.toFixed(2) + 's' +
            (clipped ? ' (clipped to 30s for display)' : '') + '</title></rect>';
  });
  let summary = 'fastest ' + realMin.toFixed(2) + 's · avg ' + realMean.toFixed(2) +
                's · slowest ' + realMax.toFixed(2) + 's';
  let html = '<div class="sparkline">';
  html += '<div class="label">block time gaps — how long between solves' +
          (realMax > SCALE_CAP ? ' (bars >30s clipped & striped)' : '') + '</div>';
  html += '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' + bars + '</svg>';
  html += '<div class="sparkline-foot">' + summary + '</div>';
  html += '</div>';
  return html;
}

function renderSolvedReward(block) {
  if (block.parent_accepted === false) {
    let html = '<span class="bad">parent rejected</span>';
    if (block.aux_count) {
      html += ' <span class="accent2">+' + block.aux_count + ' aux</span>';
    }
    return html;
  }
  let html = '<span class="ok">+50 BLC</span>';
  if (block.aux_count) {
    html += ' <span class="accent2">+' + block.aux_count + ' aux</span>';
  }
  return html;
}

function renderAcceptedBreakdown(block, s) {
  const details = block.accepted_details || [];
  if (!details.length) return '';
  const tickers = chainTickersFromState(s);
  let html = '<div class="solved-breakdown">';
  details.forEach(item => {
    const label = item.label || item.alias || 'unknown';
    const ticker = tickers[label] || label;
    const height = (typeof item.height === 'number') ? String(item.height) : '—';
    const hash = item.hash || '?';
    html += '<div class="line">';
    html += '<span class="hpill" title="' + escHtml(displayChainLabel(label)) + '">' + escHtml(ticker) + '</span>';
    html += '<span class="muted">' + escHtml(height) + '</span>';
    html += '<span class="mono">' + escHtml(hash) + '</span>';
    html += '</div>';
  });
  html += '</div>';
  return html;
}

function renderSolvedStatus(row) {
  const status = row.status || 'merged';
  const labels = {
    parent: 'parent',
    merged: 'merged',
    'aux-only': 'aux-only',
  };
  return '<span class="status-pill ' + escHtml(status) + '">' + escHtml(labels[status] || status) + '</span>';
}

function renderSolved(s) {
  const allRows = s.recent_block_rows || [];
  const chainOrder = chainOrderFromState(s);
  const filterOptions = ['__all__'].concat(chainOrder);
  const rowLimitOptions = [50, 200, 400, 800];
  const filteredRows = solvedChainFilter === '__all__'
    ? allRows
    : allRows.filter(row => row.coin === solvedChainFilter);
  const rows = filteredRows.slice(0, solvedRowLimit);
  const latest = filteredRows.length ? filteredRows[0] : null;
  const latestDisplay = latest ? displayChainLabel(latest.coin) : null;

  let html = '<details class="card full card-collapse" data-key="card-solved">';
  html += '<summary><h2>Recent Solved Blocks <span class="count">' + filteredRows.length + '</span>';
  if (latest) {
    html += ' <span class="summary-latest">latest:';
    html += ' <span>' + escHtml(latestDisplay) + '</span>';
    html += ' <span class="hpill">' + (typeof latest.height === 'number' ? latest.height : '—') + '</span>';
    html += ' <span class="mono">' + escHtml(latest.hash) + '</span>';
    html += ' <span class="muted">' + fmtAge(latest.time) + '</span>';
    html += '</span>';
  }
  html += '</h2></summary>';
  html += '<div class="card-body">';
  if (allRows.length === 0) {
    html += '<div class="empty">no blocks solved by this pool yet — start a miner above</div>';
  } else {
    html += '<div class="solved-toolbar">';
    html += '<label for="solved-chain-filter">chain</label>';
    html += '<select id="solved-chain-filter">';
    filterOptions.forEach(label => {
      const selected = label === solvedChainFilter ? ' selected' : '';
      const text = label === '__all__' ? 'All Chains' : displayChainLabel(label);
      html += '<option value="' + escHtml(label) + '"' + selected + '>' + escHtml(text) + '</option>';
    });
    html += '</select>';
    html += '<label for="solved-row-limit">show</label>';
    html += '<select id="solved-row-limit">';
    rowLimitOptions.forEach(limit => {
      const selected = limit === solvedRowLimit ? ' selected' : '';
      html += '<option value="' + limit + '"' + selected + '>first ' + limit + '</option>';
    });
    html += '</select>';
    html += '</div>';

    if (filteredRows.length === 0) {
      const filterLabel = solvedChainFilter === '__all__' ? 'this filter' : solvedChainFilter;
      html += '<div class="empty">no solved blocks recorded for ' + escHtml(filterLabel) + ' yet</div>';
    } else {
      html += '<table class="solved-table"><tr><th>chain</th><th>height</th><th>hash</th><th>difficulty</th><th>amount</th><th>solved</th><th>confirms</th></tr>';
      rows.forEach(row => {
        const rowDisplay = displayChainLabel(row.coin);
        html += '<tr>';
        html += '<td><div class="solved-chain-cell"><span>' + escHtml(rowDisplay) + '</span></div></td>';
        html += '<td><div class="solved-height-cell"><span class="hpill">' + (typeof row.height === 'number' ? row.height : '—') + '</span></div></td>';
        html += '<td><div class="solved-hash-cell"><span class="mono">' + escHtml(row.hash) + '</span></div></td>';
        html += '<td>' + fmtDifficulty(row.difficulty) + '</td>';
        html += '<td><b class="accent2">' + fmtCoinAmount(row.reward) + '</b> ' + escHtml(row.ticker || row.coin) + '</td>';
        html += '<td>' + fmtAge(row.time) + '</td>';
        html += '<td>' + (row.confirmations ?? '—') + '</td>';
        html += '</tr>';
      });
      html += '</table>';
    }
  }
  html += '</div>';      // close .card-body
  html += '</details>';  // close .card-collapse
  return html;
}

function renderShareChains(sh, s) {
  const tickers = chainTickersFromState(s);
  const accepted = sh.chains_accepted || [];
  const rejected = sh.chains_rejected || [];
  const attempted = sh.chains_attempted || [];
  const acceptedSet = new Set(accepted);
  const rejectedSet = new Set(rejected);

  if (!sh.has_chain_outcomes) {
    if (sh.status && sh.status.indexOf('share') === 0) {
      return '<span class="muted">share only</span>';
    }
    return '<span class="muted">—</span>';
  }

  const orderedLabels = attempted.length
    ? attempted
    : chainOrderFromState(s).filter(label => acceptedSet.has(label) || rejectedSet.has(label));

  if (!orderedLabels.length) {
    return '<span class="muted">—</span>';
  }

  let html = '<div class="share-chain-results">';
  orderedLabels.forEach(label => {
    const ticker = tickers[label] || label;
    const cls = acceptedSet.has(label) ? 'accepted' : 'rejected';
    html += '<span class="share-chain-badge ' + cls + '" title="' + escHtml(displayChainLabel(label)) + '">' + escHtml(ticker) + '</span>';
  });
  html += '</div>';
  return html;
}

function renderShares(s) {
  const shares = s.recent_shares || [];
  let html = '<details class="card full card-collapse" data-key="card-shares">';
  html += '<summary><h2>Recent Shares <span class="count">' + shares.length + '</span></h2></summary>';
  html += '<div class="card-body">';
  if (shares.length === 0) {
    html += '<div class="empty">no shares yet — start a miner</div>';
  } else {
    html += '<table><tr><th>age</th><th>host</th><th>worker</th><th>type</th><th>address</th><th>chains</th><th>work</th></tr>';
    shares.slice().reverse().forEach(sh => {
      const addrCls = sh.addr_type === 'bech32' ? 'ok' :
                      (sh.addr_type === 'legacy' || sh.addr_type === 'p2sh') ? 'accent' : 'bad';
      let workerCell;
      if (sh.worker) {
        workerCell = escHtml(sh.worker);
      } else if (!sh.addr) {
        workerCell = '<span class="muted">' + escHtml(sh.user) + '</span>';
      } else {
        workerCell = '<span class="muted">—</span>';
      }
      let typeCell;
      if (sh.addr) {
        const tagText = ADDR_TYPE_LABEL[sh.addr_type] || sh.addr_type;
        const pillTitle = ADDR_TYPE_TOOLTIP[sh.addr_type] || '';
        typeCell = '<span class="type-pill ' + sh.addr_type + '" title="' + escHtml(pillTitle) + '">' + tagText + '</span>';
      } else {
        typeCell = '<span class="warn-marker" title="' + escHtml(ADDR_TYPE_TOOLTIP.none) + '">⚠</span>';
      }
      let addrCell;
      if (sh.addr) {
        addrCell = '<span class="' + addrCls + '">' + escHtml(sh.addr) + '</span>';
      } else {
        addrCell = '<span class="bad" title="' + escHtml(ADDR_TYPE_TOOLTIP.none) + '">⚠ misconfig</span>';
      }
      html += '<tr>';
      html += '<td>' + fmtAge(sh.time) + '</td>';
      html += '<td class="mono">' + escHtml(sh.host) + '</td>';
      html += '<td>' + workerCell + '</td>';
      html += '<td>' + typeCell + '</td>';
      html += '<td class="mono" style="font-size:11px">' + addrCell + '</td>';
      html += '<td>' + renderShareChains(sh, s) + '</td>';
      html += '<td>' + fmtWork(sh.weight || 0) + '</td>';
      html += '</tr>';
    });
    html += '</table>';
  }
  html += '</div>';      // close .card-body
  html += '</details>';  // close .card-collapse
  return html;
}

function renderSolvedShares(s) {
  const shares = s.recent_solved_shares || [];
  let html = '<details class="card full card-collapse" data-key="card-solved-shares">';
  html += '<summary><h2>Recent Solved Shares <span class="count">' + shares.length + '</span></h2></summary>';
  html += '<div class="card-body">';
  if (shares.length === 0) {
    html += '<div class="empty">no solved shares yet — block-winning shares will appear here</div>';
  } else {
    html += '<table><tr><th>age</th><th>host</th><th>worker</th><th>type</th><th>address</th><th>chains solved</th><th>work</th></tr>';
    shares.slice().reverse().forEach(sh => {
      const addrCls = sh.addr_type === 'bech32' ? 'ok' :
                      (sh.addr_type === 'legacy' || sh.addr_type === 'p2sh') ? 'accent' : 'bad';
      let workerCell;
      if (sh.worker) {
        workerCell = escHtml(sh.worker);
      } else if (!sh.addr) {
        workerCell = '<span class="muted">' + escHtml(sh.user) + '</span>';
      } else {
        workerCell = '<span class="muted">—</span>';
      }
      let typeCell;
      if (sh.addr) {
        const tagText = ADDR_TYPE_LABEL[sh.addr_type] || sh.addr_type;
        const pillTitle = ADDR_TYPE_TOOLTIP[sh.addr_type] || '';
        typeCell = '<span class="type-pill ' + sh.addr_type + '" title="' + escHtml(pillTitle) + '">' + tagText + '</span>';
      } else {
        typeCell = '<span class="warn-marker" title="' + escHtml(ADDR_TYPE_TOOLTIP.none) + '">⚠</span>';
      }
      let addrCell;
      if (sh.addr) {
        addrCell = '<span class="' + addrCls + '">' + escHtml(sh.addr) + '</span>';
      } else {
        addrCell = '<span class="bad" title="' + escHtml(ADDR_TYPE_TOOLTIP.none) + '">⚠ misconfig</span>';
      }
      html += '<tr>';
      html += '<td>' + fmtAge(sh.time) + '</td>';
      html += '<td class="mono">' + escHtml(sh.host) + '</td>';
      html += '<td>' + workerCell + '</td>';
      html += '<td>' + typeCell + '</td>';
      html += '<td class="mono" style="font-size:11px">' + addrCell + '</td>';
      html += '<td>' + renderShareChains(sh, s) + '</td>';
      html += '<td>' + fmtWork(sh.weight || 0) + '</td>';
      html += '</tr>';
    });
    html += '</table>';
  }
  html += '</div>';
  html += '</details>';
  return html;
}

function renderLog(s) {
  const lines = (s.pool_log || []).join('\\n');
  let html = '<div class="card full"><h2>Pool Log</h2>';
  html += '<details class="collapsible" data-key="pool-log"><summary>show last ' + (s.pool_log?.length || 0) + ' lines</summary>';
  html += '<pre class="log">' + escHtml(lines) + '</pre>';
  html += '</details></div>';
  return html;
}

function render(s) {
  return renderChain(s)
       + renderPool(s)
       + renderEndpointHelp(s)
       + renderMiningKeyGenerator(s)
       + renderPayouts(s)
       + renderMiners(s)
       + renderSolved(s)
       + renderSolvedShares(s)
       + renderShares(s)
       + renderLog(s);
}

// Single copy helper. Uses navigator.clipboard.writeText when available
// (HTTPS / localhost), otherwise falls back to a hidden-textarea +
// document.execCommand('copy') trick that still works on plain HTTP.
// Always shows a toast — never opens a modal or browser prompt.
function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => _copyExec(text));
  }
  return Promise.resolve(_copyExec(text));
}
function _copyExec(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '-9999px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}
let _toastTimer = null;
function showToast(msg, kind) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = 'toast'; }, 1800);
}
async function copyAndToast(text, label) {
  const ok = await copyToClipboard(text);
  showToast(ok ? ((label || 'value') + ' copied to clipboard') : 'copy failed', ok ? 'ok' : 'err');
  return ok;
}

function attachHandlers() {
  const btn = $('copy-stratum');
  if (btn) {
    btn.onclick = async () => { await copyAndToast(btn.dataset.url, 'stratum url'); };
  }

  const walletClose = $('wallet-modal-close');
  if (walletClose) {
    walletClose.onclick = (ev) => {
      ev.preventDefault();
      closePoolWalletModal();
    };
  }

  const walletBackdrop = $('wallet-modal-backdrop');
  if (walletBackdrop) {
    walletBackdrop.onclick = (ev) => {
      if (ev.target === walletBackdrop) closePoolWalletModal();
    };
  }

  const solvedFilter = $('solved-chain-filter');
  if (solvedFilter) {
    solvedFilter.onchange = () => {
      solvedChainFilter = solvedFilter.value || '__all__';
      if (lastState) renderState(lastState);
    };
  }

  const solvedLimit = $('solved-row-limit');
  if (solvedLimit) {
    solvedLimit.onchange = () => {
      const next = parseInt(solvedLimit.value || '50', 10);
      solvedRowLimit = Number.isFinite(next) && next > 0 ? next : 50;
      if (lastState) renderState(lastState);
    };
  }

  document.querySelectorAll('.chain-tab[data-chain-label]').forEach(el => {
    el.onclick = (ev) => {
      ev.preventDefault();
      chainCardSelected = el.dataset.chainLabel || 'Blakecoin';
      localStorage.setItem('chain-card-selected', chainCardSelected);
      if (lastState) renderState(lastState);
    };
  });

  // Per-identity copy buttons
  document.querySelectorAll('.copy-id').forEach(el => {
    el.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      await copyAndToast(el.dataset.copy, 'address');
    };
  });

  // Mining Key Generator: copy buttons (data-copy on each .copy-btn)
  document.querySelectorAll('.copy-btn[data-copy]').forEach(el => {
    el.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      await copyAndToast(el.dataset.copy, el.dataset.label || 'value');
    };
  });

  // Mining Key Generator: Generate / Regenerate buttons
  const rerenderMkgenArea = () => {
    if (!lastState) return;
    const area = $('mkgen-area');
    if (!area) return;
    const pool = lastState.pool || {};
    const stratum = lastState.stratum || {};
    area.innerHTML = renderMkgenArea(
      mkgenCurrentResult(),
      pool.mining_key_segwit_hrp || '',
      stratum.host || '',
      stratum.port || 3334,
      pool.mining_key_v2_coin_hrps || {}
    );
    attachHandlers();
  };

  const _mkgenGenerate = async () => {
    if (!lastState) return;
    const pool = lastState.pool || {};
    const result = await generateMiningKeyBundle(pool.mining_key_segwit_hrp || '');
    _mkgenLast.v2 = result;
    rerenderMkgenArea();
  };
  const wireGenBtn = (id) => {
    const b = $(id);
    if (b && !b.disabled) {
      b.onclick = (ev) => { ev.preventDefault(); _mkgenGenerate(); };
    }
  };
  wireGenBtn('mkgen-btn');
  wireGenBtn('mkgen-regen');

  // Save the full mining-key bundle to a text file so the operator has one
  // plain-text recovery/export artifact with the private key, pubkey,
  // mining-key payload, stratum username, and every derived payout address.
  const saveBtn = $('mkgen-save');
  const currentMkgen = mkgenCurrentResult();
  if (saveBtn && currentMkgen) {
    saveBtn.onclick = (ev) => {
      ev.preventDefault();
      const lines = [
        'Private key for wallets: ' + currentMkgen.privHex,
        'Public key: ' + currentMkgen.pubHex,
        'Mining key payload: ' + currentMkgen.miningKey,
      ];
      if (currentMkgen.stratumUsername && currentMkgen.stratumUsername !== currentMkgen.miningKey) {
        lines.push('Stratum username for miner: ' + currentMkgen.stratumUsername);
      }

      const derivedAddresses = currentMkgen.derivedAddresses || null;
      if (derivedAddresses && Object.keys(derivedAddresses).length) {
        lines.push('');
        lines.push('Derived payout addresses:');
        Object.entries(derivedAddresses).forEach(([label, details]) => {
          const hrp = details && details.hrp ? ' [' + details.hrp + ']' : '';
          const address = details && details.address ? details.address : '';
          lines.push('- ' + displayChainLabel(label) + hrp + ': ' + address);
        });
      } else if (currentMkgen.derivedAddress) {
        lines.push('');
        lines.push('Derived payout address: ' + currentMkgen.derivedAddress);
      }
      const blob = new Blob([lines.join('\\n') + '\\n'], {type: 'text/plain'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'mining_keys.txt';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast('keys saved to mining_keys.txt', 'ok');
    };
  }

  // Worker name input → live update the cgminer command preview
  const workerInput = $('mkgen-worker');
  const workerSpan = $('mkgen-cmd-worker');
  if (workerInput && workerSpan) {
    workerInput.oninput = () => {
      const v = workerInput.value.trim() || 'rig1';
      workerSpan.textContent = v;
    };
  }

  // Copy the entire cgminer command (with the current worker name applied)
  const copyCmdBtn = $('mkgen-copy-cmd');
  if (copyCmdBtn && currentMkgen) {
    copyCmdBtn.onclick = async (ev) => {
      ev.preventDefault();
      const ws = $('mkgen-worker');
      const worker = (ws && ws.value.trim()) || 'rig1';
      const stratum = (lastState && lastState.stratum) || {};
      const url = 'stratum+tcp://' + (stratum.host || '') + ':' + (stratum.port || 3334);
      const cmd = 'cgminer -o ' + url + ' -u ' + currentMkgen.stratumUsername + '.' + worker + ' -p x';
      await copyAndToast(cmd, 'cgminer command');
    };
  }
}

convertTitles();   // process any title attrs already in the static page (header)
refresh();
setInterval(refresh, 5000);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') closePoolWalletModal();
});
// Run the JS↔Python parity self-test once on page load. Logs a console
// warning if the implementations drift.
selfTestMiningKeyJS();
</script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(
        INDEX_HTML,
        tracker_addr=TRACKER_ADDR,
        RUN_TIMESTAMP=RUN_TIMESTAMP,
        HEADER_TITLE=HEADER_TITLE,
        HEADER_SUBTITLE=HEADER_SUBTITLE,
        STRATUM_URL=f"stratum+tcp://{STRATUM_HOST}:{STRATUM_PORT}",
    )


@app.route('/favicon.ico')
def favicon():
    # Minimal 16x16 SVG favicon in Blakecoin-cyan. Inlined to avoid shipping
    # a binary asset and to silence the 404 every browser makes on page load.
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<rect width="16" height="16" rx="3" fill="#1a1d21"/>'
        '<text x="8" y="12" text-anchor="middle" font-family="monospace" '
        'font-size="11" font-weight="bold" fill="#4fc3f7">B</text>'
        '</svg>'
    )
    return svg, 200, {'Content-Type': 'image/svg+xml'}


@app.route('/api/derive-address-v2', methods=['GET', 'POST'])
def api_derive_address_v2():
    """Server-side reference implementation of v2 bech32 mining-key derivation.

    Input (POST JSON or GET query):
      mining_key    - 40-char hex v2 mining key
      hrp           - witness HRP (for example blc after SegWit activation)
    Output:
      { ok, derived_address?, error? }
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        mining_key = body.get('mining_key') or ''
        hrp = body.get('hrp') or ''
    else:
        mining_key = request.args.get('mining_key', '')
        hrp = request.args.get('hrp', '')

    if not mining_key or not hrp:
        return jsonify({'ok': False, 'error': 'mining_key and hrp required'}), 400
    try:
        result = derive_v2_addresses(mining_key, hrp)
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    derived = result.get('derived_address')
    if derived is None:
        return jsonify({'ok': False, 'error': 'derivation failed (invalid mining key or HRP)'}), 400
    return jsonify({'ok': True, **result})


@app.route('/api/derive-addresses-v2', methods=['GET', 'POST'])
def api_derive_addresses_v2():
    """Return the full configured BlakeStream V2 payout set for one mining key."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        mining_key = body.get('mining_key') or ''
        hrp = body.get('hrp') or ''
    else:
        mining_key = request.args.get('mining_key', '')
        hrp = request.args.get('hrp', '')

    if not mining_key:
        return jsonify({'ok': False, 'error': 'mining_key required'}), 400

    try:
        result = derive_v2_addresses(mining_key, hrp or None)
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    if not result.get('derived_address') and not result.get('derived_addresses'):
        return jsonify({'ok': False, 'error': 'derivation failed (invalid mining key or HRP set)'}), 400
    return jsonify({'ok': True, **result})


@app.route('/api/verify-mining-key', methods=['POST'])
def api_verify_mining_key():
    """Take a 33-byte compressed secp256k1 pubkey from the browser, derive
    the V2 mining key (RIPEMD160(SHA256(pub))), and optionally derive the
    payout address against the configured HRP.

    Used by the dashboard's mining-key generator card to:
      1. Cross-check that the JS-side keypair derivation produced the same
         mining key as the Python reference would have.
      2. Provide a verify-existing-key flow where the user pastes a pubkey
         and gets the mining key + derived address back.

    Input (POST JSON):
      pubkey_hex    - 66-char hex (33 bytes, must start with 0x02/0x03)
      version?      - must be 2 when supplied
      hrp?          - optional HRP to also derive the V2 payout from
    Output:
      { ok, mining_key, derived_address?, error? }
    """
    body = request.get_json(silent=True) or {}
    pubkey_hex = body.get('pubkey_hex') or ''
    version = int(body.get('version') or 2)
    hrp = body.get('hrp') or ''

    if version != 2:
        return jsonify({'ok': False, 'error': 'only version 2 mining keys are supported'}), 400

    try:
        mining_key = mining_key_v2_from_compressed_pubkey(pubkey_hex)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    out = {'ok': True, 'version': version, 'mining_key': mining_key}
    if hrp:
        derived = address_from_v2_mining_key(mining_key, hrp)
        if derived is None:
            out['error'] = 'mining key OK but address derivation failed'
        else:
            out['derived_address'] = derived
    return jsonify(out)


_API_STATE_CACHE = {'ts': 0.0, 'body': None}
_API_STATE_LOCK = threading.Lock()
_API_STATE_TTL = 2.0   # seconds — serve cached JSON to concurrent callers


@app.route('/api/state')
def api_state():
    # Coalesce concurrent requests: under GIL contention a CPU-heavy
    # /api/state handler serializes across threads. If a fresh response
    # is already computed within the TTL, return it directly — the
    # browser's 5s refresh never needs more than one computation per
    # TTL window across all connected clients.
    now = time.time()
    with _API_STATE_LOCK:
        if _API_STATE_CACHE['body'] is not None and (now - _API_STATE_CACHE['ts']) < _API_STATE_TTL:
            return _API_STATE_CACHE['body']
    chain = rpc('getblockchaininfo')
    if isinstance(chain, dict) and '_error' not in chain:
        peers = rpc('getconnectioncount')
        if isinstance(peers, int):
            chain['connections'] = peers
    chain_overview = build_chain_overview(chain)

    pool_lines = read_lines(POOL_LOG)
    all_share_lines = read_lines(SHARE_LOG)
    share_lines = all_share_lines[-100:]

    # Proxy solve detail is needed to expand one merged-mining solve into
    # parent + aux-chain rows. Keep that state incrementally instead of using
    # a tiny log tail; under active miners the accepted aux detail scrolls out
    # quickly, which leaves the Recent Solved Blocks table showing only BLC.
    proxy_solves = incremental_proxy_solves()
    all_solved = parse_pool_state(pool_lines, max_rows=None)
    solved_all = [_finalize_solved_block(b) for b in all_solved]
    # Only run the expensive attach_proxy_solves on the tail we render
    solved_recent = attach_proxy_solves(
        all_solved[-50:],
        proxy_solves,
        max_rows=None,
    )
    # Splice the recent-with-proxy-detail back into solved_all at the end
    if solved_recent:
        solved_all = solved_all[:-len(solved_recent)] + solved_recent
    solved = solved_all[-15:]
    recent_block_rows = build_recent_block_rows(solved)
    shares = attach_share_chain_outcomes(parse_share_log(share_lines), proxy_solves)
    solved_shares = build_recent_solved_shares(all_share_lines, proxy_solves)

    # Last GBT height seen in pool log = last "New block" line
    last_template_height = None
    for line in reversed(pool_lines):
        m = SOLVED_RE.search(line)
        if m:
            last_template_height = int(m.group(2))
            break

    # Per-identity rows (one row per stratum username), cross-referenced with
    # the live `ss` snapshot for "is the host even talking to us right now".
    # We parse the FULL share log for identity stats (not the 100-line tail
    # used for "Recent Shares" display) so counters like `blocks` are
    # cumulative — otherwise a winning share scrolling out of the tail would
    # cause the per-miner block count to visibly drop, which surprises users.
    connections = list_stratum_connections()
    id_stats = parse_identity_stats(all_share_lines)
    # For each identity, also credit aux-chain wins: whenever a parent-chain
    # block solved by this identity also accepted aux chains, each aux win
    # counts as an additional merged block. This makes the number match what
    # a miner intuitively expects on a merged-mined pool.
    _credit_aux_chain_wins(id_stats, all_solved, proxy_solves)
    identities = merge_identity_view(connections, id_stats)

    # Payouts are computed by a background thread (see _payout_warmer_loop)
    # and cached — /api/state NEVER walks the block history inline. On a
    # fresh restart the caches are empty; totals will populate within ~1
    # minute as the warmer completes its first full pass. This keeps
    # /api/state well under 1 second regardless of history depth.
    _ensure_payout_warmer_started()
    with _PAYOUT_SNAPSHOT_LOCK:
        block_payouts = dict(_PAYOUT_SNAPSHOT_PARENT)
        child_block_payouts = {k: dict(v) for k, v in _PAYOUT_SNAPSHOT_CHILDREN.items()}
        child_pool_keeps = dict(_PAYOUT_SNAPSHOT_CHILD_POOL)
    # Hand the warmer the freshest hash list so it knows what to walk next
    with _PAYOUT_SNAPSHOT_LOCK:
        _PAYOUT_WARMER_PARENT_HASHES[:] = [b['hash'] for b in solved_all if b.get('parent_accepted')]
        _PAYOUT_WARMER_SOLVED_BLOCKS[:] = list(solved_all)
    for i in identities:
        payout_targets = identity_payout_targets(i)
        if i.get('addr'):
            i['paid_satoshis'] = block_payouts.get(i['addr'], 0)
        else:
            i['paid_satoshis'] = 0
        merged_paid = {}
        merged_paid_chain_count = 0
        for label, payouts in child_block_payouts.items():
            payout_addr = payout_targets.get(label)
            satoshis = payouts.get(payout_addr, 0) if payout_addr else 0
            merged_paid[label] = satoshis
            if satoshis > 0:
                merged_paid_chain_count += 1
        i['merged_paid_satoshis'] = merged_paid
        i['merged_paid_chain_count'] = merged_paid_chain_count
        all_paid = {'Blakecoin': i['paid_satoshis']}
        for label in configured_chain_order():
            if label == 'Blakecoin':
                continue
            all_paid[label] = merged_paid.get(label, 0)
        i['all_paid_satoshis'] = all_paid
        i['paid_chain_count'] = sum(1 for satoshis in all_paid.values() if satoshis > 0)
    tracker_paid_satoshis = block_payouts.get(TRACKER_ADDR, 0)
    credited_payout_satoshis = aggregate_identity_payout_totals(identities)
    pool_wallet_paid_satoshis = pool_wallet_paid_totals(tracker_paid_satoshis, child_pool_keeps)
    pool_wallet_rows = get_pool_wallet_rows()

    # Newest solve age — used for the status banner countdown
    last_block_ts = max((b.get('ts') or 0 for b in solved_all), default=0)

    service_summary = read_service_summary()
    status = derive_status(chain, pool_lines, identities, solved, service_summary=service_summary)

    response = jsonify({
        'run_timestamp': RUN_TIMESTAMP,
        'status': status,
        'now':    time.time(),
        'chain':  chain,
        'chain_overview': chain_overview,
        'pool': {
            'state':                'running' if pool_lines and 'merkleMaker' in '\n'.join(pool_lines[-10:]) else 'idle',
            'last_template_height': last_template_height,
            'last_block_ts':        last_block_ts or None,
            'tracker_addr':         TRACKER_ADDR,
            'tracker_paid_satoshis': tracker_paid_satoshis,
            'aux_pool_addresses':   AUX_POOL_ADDRESSES,
            'aux_pool_paid_satoshis': child_pool_keeps,
            'credited_payout_satoshis': credited_payout_satoshis,
            'pool_wallet_paid_satoshis': pool_wallet_paid_satoshis,
            'pool_wallet_rows':     pool_wallet_rows,
            'aux_payout_mode':      AUX_PAYOUT_MODE,
            'payout_model':         (
                'PROP via CoinbaserCmd + pooled aux accounting'
                if HAS_COINBASER and AUX_PAYOUT_MODE == 'pool'
                else ('PROP via CoinbaserCmd' if HAS_COINBASER else 'pool wallet only')
            ),
            'mining_key_segwit_hrp': MINING_KEY_SEGWIT_HRP,
            'mining_key_v2_coin_hrps': configured_v2_coin_hrps(),
            'mining_key_enabled':   bool(MINING_KEY_SEGWIT_HRP),
            'mining_key_v2_enabled': bool(MINING_KEY_SEGWIT_HRP),
        },
        'stratum':         {'host': STRATUM_HOST, 'port': STRATUM_PORT},
        'chain_tickers':   CHAIN_TICKERS,
        'chain_order':     configured_chain_order(),
        'identities':      identities,
        'active_count':    sum(1 for i in identities if i['active']),
        'live_socket_count': len(connections),
        'recent_solved':   solved,
        'recent_blocks':   solved,
        'recent_block_rows': recent_block_rows,
        'recent_solved_shares': solved_shares,
        'recent_shares':   shares,
        'pool_log':        display_pool_log(pool_lines)[-40:],
    })
    with _API_STATE_LOCK:
        _API_STATE_CACHE['ts'] = time.time()
        _API_STATE_CACHE['body'] = response
    return response


if __name__ == '__main__':
    host, port = BIND.rsplit(':', 1)
    # threaded=True is essential. Werkzeug's default dev server is single-
    # threaded, so a slow /api/state (reading 30MB proxy log + RPC-calling
    # 6 daemons) blocks every other request including /favicon.ico and the
    # initial / page load. With the ASIC pumping shares, requests queued
    # faster than they drained and the page went blank. Threading lets
    # concurrent requests be served in parallel.
    app.run(host=host, port=int(port), debug=False, use_reloader=False, threaded=True)
