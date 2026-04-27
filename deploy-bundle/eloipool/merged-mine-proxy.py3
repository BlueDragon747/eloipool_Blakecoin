#!/usr/bin/env python3
# merged-mine-proxy - Python 3.10 Version based on namecoin's merged-mine-proxy a clean conversion from Python 2.7 
# originally under MIT Licence see COPYING https://github.com/BlueDragon747/eloipool_Blakecoin/blob/master/COPYING
# Copyright (c) 2013-2026 The Blakecoin developers
# Portions written by BlueDragon747 for the Blakecoin Project

import logging
import argparse
import os
import sys
import traceback
import json
import base64
from binascii import a2b_hex, b2a_hex
import socket
from datetime import datetime
from time import sleep, time

from twisted.internet import defer, reactor, task, threads
from twisted.web import server, resource
from twisted.internet.error import ConnectionRefusedError
import twisted.internet.error
from urllib.parse import urlsplit
import http.client as httplib
import _thread

try:
    from mining_key import resolve_payout_address
except Exception:
    resolve_payout_address = None

# Setup logging first
logger = logging.getLogger('merged-mine-proxy')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

try:
    from merkletree import MerkleTree
    have_merkletree = True
except ImportError:
    have_merkletree = False
    logger.warning("merkletree module not found, using simple mode")
    class MerkleTree:
        def __init__(self, L, detailed=False):
            self.detail = L

__version__ = '0.2.4-py3'

AUX_UPDATE_INTERVAL = 1
MERKLE_TREES_TO_KEEP = 240
# Keep per-solver aux templates very fresh on fast devnet runs so queued child
# mempool transactions have a chance to land before the next merged solve.
AUX_SOLVER_CACHE_TTL = 1

# Chain name mapping for logging (matches database naming)
CHAIN_NAMES = {0: 'MM', 1: 'MM1', 2: 'MM3', 3: 'MM4', 4: 'MM5'}

def get_chain_name(chain_idx):
    """Get display name for chain index"""
    return CHAIN_NAMES.get(chain_idx, 'MM%d' % chain_idx)


@defer.inlineCallbacks
def fetch_chain_acceptance_details(proxy, chain_idx, payout_address=None):
    """Capture the accepted child tip immediately after a successful submit.

    We log this alongside the solve so the dashboard can render each accepted
    chain with its own height/hash instead of only showing a merged pill list.
    """
    try:
        best_hash = yield proxy.rpc_getbestblockhash()
        block = yield proxy.rpc_getblock(best_hash, True)
        defer.returnValue({
            'chain_idx': chain_idx,
            'alias': get_chain_name(chain_idx),
            'height': int(block['height']),
            'hash': str(best_hash),
            'tx_count': len(block.get('tx', [])),
            'payout_address': payout_address,
        })
    except Exception as e:
        logger.warning("%s: Failed to capture acceptance details: %s", get_chain_name(chain_idx), e)
        defer.returnValue({
            'chain_idx': chain_idx,
            'alias': get_chain_name(chain_idx),
            'height': None,
            'hash': None,
            'tx_count': None,
            'payout_address': payout_address,
            'error': str(e),
        })

def reverse_chunks(s, l):
    """Reverse string in chunks"""
    return ''.join(reversed([s[x:x+l] for x in range(0, len(s), l)]))

def getresponse(http, path, postdata, headers):
    """Make HTTP POST request and return response"""
    http.request('POST', path, postdata, headers)
    return http.getresponse().read()

class Error(Exception):
    def __init__(self, code, message, data=''):
        if not isinstance(code, int):
            raise TypeError('code must be an int')
        if not isinstance(message, str):
            raise TypeError('message must be a str')
        self._code, self._message, self._data = code, message, data
    def __str__(self):
        return '%i %s %r' % (self._code, self._message, self._data)
    def _to_obj(self):
        return {
            'code': self._code,
            'message': self._message,
            'data': self._data,
        }

class Proxy(object):
    MAX_RETRIES = 3
    CONNECT_TIMEOUT = 30
    IDLE_TIMEOUT = 1800  # Force reconnect after 30 minutes idle
    MAX_CONSECUTIVE_FAILURES = 5  # Circuit breaker threshold
    BREAKER_COOLDOWN_S = 30  # Half-open probe after this many seconds
    
    def __init__(self, url):
        (schema, netloc, path, query, fragment) = urlsplit(url)
        auth = None
        if netloc.find('@') >= 0:
            (auth, netloc) = netloc.split("@")
        if path == "":
            path = "/"
        self._url = "%s://%s%s" % (schema, netloc, path)
        self._path = path
        self._auth = auth
        self._netloc = netloc
        self._http = None
        self._last_used = 0
        self._consecutive_failures = 0
        self._breaker_opened_at = 0  # epoch when breaker tripped open
        self._total_requests = 0
    
    def _connect(self):
        """Establish fresh connection with proper cleanup"""
        if self._http is not None:
            try:
                self._http.close()
            except:
                pass
            self._http = None
        
        (host, port) = self._netloc.split(":")
        logger.debug("Connecting to %s:%s", host, port)
        self._http = httplib.HTTPConnection(host, int(port), timeout=self.CONNECT_TIMEOUT)
        self._http.connect()
        self._consecutive_failures = 0  # Reset on successful connect
    
    def _is_connection_stale(self):
        """Check if connection has been idle too long"""
        if self._http is None:
            return True
        if self._total_requests == 0:
            return False  # First request
        
        idle_time = time() - self._last_used
        if idle_time > self.IDLE_TIMEOUT:
            logger.info("Connection idle for %d seconds, forcing reconnect", idle_time)
            return True
        return False
    
    def _call_remote_single(self, method, params):
        """Single attempt at RPC call"""
        # Check staleness
        if self._is_connection_stale():
            logger.debug("Connection stale, reconnecting...")
            self._connect()
        
        if self._http is None:
            self._connect()
        
        id_ = 0
        
        headers = {
            'Content-Type': 'application/json',
        }
        if self._auth is not None:
            # Python 3 fix: encode auth string to bytes
            auth_bytes = self._auth.encode('utf-8') if isinstance(self._auth, str) else self._auth
            auth_b64 = base64.b64encode(auth_bytes).decode('utf-8')
            headers['Authorization'] = 'Basic ' + auth_b64
        
        postdata = json.dumps({
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': id_,
        })

        content = getresponse(self._http, self._path, postdata, headers)
        
        resp = json.loads(content.decode('utf-8'))
        
        if resp['id'] != id_:
            raise ValueError('invalid id')
        if 'error' in resp and resp['error'] is not None:
            raise Error(resp['error']['code'], resp['error']['message'])
        
        # Success - update tracking
        self._last_used = time()
        self._total_requests += 1
        return resp['result']
    
    def callRemote(self, method, *params):
        """Call remote method with retry logic and circuit breaker"""
        # Circuit breaker check (with half-open self-heal)
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            elapsed = time() - self._breaker_opened_at
            if elapsed < self.BREAKER_COOLDOWN_S:
                logger.error("Circuit breaker open for %s - too many failures", self._url)
                raise Error(-32099, 'Circuit breaker open - too many connection failures', self._url)
            # Cooldown elapsed — half-open: let one probe through.
            logger.info("Circuit breaker half-open for %s — probing after %ds cooldown", self._url, int(elapsed))
        
        last_error = None
        
        for attempt in range(self.MAX_RETRIES):
            try:
                result = self._call_remote_single(method, params)
                # Success - reset failure counter
                self._consecutive_failures = 0
                return result
                
            except (httplib.HTTPException, socket.error, OSError, ConnectionError) as e:
                last_error = e
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._breaker_opened_at = time()
                logger.warning("RPC call failed (attempt %d/%d): %s", 
                               attempt + 1, self.MAX_RETRIES, e)
                
                # Close connection and retry
                try:
                    if self._http:
                        self._http.close()
                except:
                    pass
                self._http = None
                
                if attempt < self.MAX_RETRIES - 1:
                    # Wait before retry (exponential backoff)
                    wait_time = min(0.1 * (2 ** attempt), 2.0)
                    logger.debug("Waiting %.1fs before retry...", wait_time)
                    sleep(wait_time)
                    # Try to reconnect
                    try:
                        self._connect()
                    except Exception as connect_error:
                        logger.warning("Reconnection failed: %s", connect_error)
                        continue
                
            except Error as e:
                # JSON-RPC error - don't retry, just re-raise
                raise
            except Exception as e:
                # Unexpected error - log and retry
                last_error = e
                logger.error("Unexpected error in callRemote: %s", e)
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._breaker_opened_at = time()
                if attempt < self.MAX_RETRIES - 1:
                    sleep(0.1)
        
        # All retries exhausted
        logger.error("All %d RPC attempts failed for %s", self.MAX_RETRIES, self._url)
        raise Error(-32099, 'Could not connect to backend after %d retries: %s' % 
                   (self.MAX_RETRIES, last_error), self._url)
    
    def __getattr__(self, attr):
        if attr.startswith('rpc_'):
            return lambda *params: self.callRemote(attr[len('rpc_'):], *params)
        raise AttributeError('%r object has no attribute %r' % (self.__class__.__name__, attr))

class Server(resource.Resource):
    extra_headers = None
    
    def render(self, request):
        def finish(x):
            if request._disconnected:
                return
            if x is not None:
                request.write(x)
            request.finish()
        
        def finish_error(fail):
            if request._disconnected:
                return
            request.setResponseCode(500)
            request.write(b'---ERROR---')
            request.finish()
            fail.printTraceback()
        
        defer.maybeDeferred(resource.Resource.render, self, request).addCallbacks(finish, finish_error)
        return server.NOT_DONE_YET

    @defer.inlineCallbacks
    def render_POST(self, request):
        data = request.content.read()
        
        if self.extra_headers is not None:
            for name, value in self.extra_headers.items():
                request.setHeader(name, value)
        
        try:
            try:
                req = json.loads(data.decode('utf-8'))
            except Exception:
                raise Error(-32700, 'Parse error')
        except Error as e:
            # id unknown
            request.write(json.dumps({
                'jsonrpc': '2.0',
                'id': None,
                'result': None,
                'error': e._to_obj(),
            }).encode('utf-8'))
            request.finish()
            return
        
        id_ = req.get('id', None)
        
        try:
            try:
                method = req['method']
                if not isinstance(method, str):
                    raise ValueError()
                params = req.get('params', [])
                if not isinstance(params, list):
                    raise ValueError()
            except Exception:
                raise Error(-32600, 'Invalid Request')
            
            method_name = 'rpc_' + method
            if not hasattr(self, method_name):
                raise Error(-32601, 'Method not found')
            method_meth = getattr(self, method_name)
            
            df = defer.maybeDeferred(method_meth, *params)
            
            if id_ is None:
                return
            
            try:
                result = yield df
            except Exception as e:
                logger.error(str(e))
                raise Error(-32099, 'Unknown error: ' + str(e))
            
            res = json.dumps({
                'jsonrpc': '2.0',
                'id': id_,
                'result': result,
                'error': None,
            })
            request.setHeader('content-length', str(len(res)))
            request.write(res.encode('utf-8'))
        except Error as e:
            res = json.dumps({
                'jsonrpc': '2.0',
                'id': id_,
                'result': None,
                'error': e._to_obj(),
            })
            request.setHeader('content-length', str(len(res)))
            request.write(res.encode('utf-8'))

class Listener(Server):
    def __init__(self, parent, auxs, merkle_size, rewrite_target, aux_payout_addresses=None, per_solver_aux_payouts=False):
        Server.__init__(self)
        self.parent = parent
        self.auxs = auxs
        self.aux_payout_addresses = list(aux_payout_addresses or [])
        self.per_solver_aux_payouts = bool(per_solver_aux_payouts)
        if len(self.aux_payout_addresses) < len(self.auxs):
            self.aux_payout_addresses.extend([None] * (len(self.auxs) - len(self.aux_payout_addresses)))
        elif len(self.aux_payout_addresses) > len(self.auxs):
            raise ValueError('Too many aux payout addresses supplied')
        self.chain_ids = [None for i in auxs]
        self.aux_targets = [None for i in auxs]
        self.chain_health = {}  # chain_idx -> {'healthy': bool, 'last_success': time, 'failures': count}
        self.per_solver_cache = {}
        self.merkle_size = merkle_size
        self.merkle_tree_queue = []
        self.merkle_trees = {}
        self.rewrite_target = None
        if rewrite_target == 32:
            self.rewrite_target = reverse_chunks("0000000007ffffffffffffffffffffffffffffffffffffffffffffffffffffff", 2)
        elif rewrite_target == 100:
            self.rewrite_target = reverse_chunks("00000000028f5c28000000000000000000000000000000000000000000000000", 2)
        if merkle_size > 100:
            raise ValueError('merkle size up to 255')
        # Python 3 fix: use bytes for empty path
        self.putChild(b'', self)
        # Start aux update process
        self.update_aux_process()
        # Start periodic status reporting
        self.status_report_process()

    def _get_aux_payout_address(self, chain_idx):
        if chain_idx >= len(self.aux_payout_addresses):
            return None
        addr = self.aux_payout_addresses[chain_idx]
        if addr is None:
            return None
        addr = str(addr).strip()
        return addr or None

    def _normalize_payout_addresses(self, payout_addresses=None):
        normalized = []
        for chain_idx in range(len(self.auxs)):
            addr = None
            if payout_addresses is not None and chain_idx < len(payout_addresses):
                raw = payout_addresses[chain_idx]
                if raw is not None:
                    raw = str(raw).strip()
                    if raw:
                        addr = raw
            if addr is None:
                addr = self._get_aux_payout_address(chain_idx)
            normalized.append(addr)
        return tuple(normalized)

    @staticmethod
    def _infer_hrp(address):
        if not address:
            return None
        address = str(address).strip()
        if '1' not in address:
            return None
        hrp, _sep, _rest = address.partition('1')
        return hrp or None

    def _resolve_selector_payout_addresses(self, selector):
        payout_addresses = None
        username = None
        if isinstance(selector, dict):
            payout_addresses = selector.get('payout_addresses')
            username = selector.get('username') or selector.get('stratum_username')
        elif selector not in (None, ''):
            username = selector

        if payout_addresses is not None:
            return self._normalize_payout_addresses(payout_addresses)

        base_addresses = list(self._normalize_payout_addresses(None))
        if not self.per_solver_aux_payouts:
            return tuple(base_addresses)
        if not username or resolve_payout_address is None:
            return tuple(base_addresses)

        resolved_any = False
        for chain_idx, base_addr in enumerate(base_addresses):
            hrp = self._infer_hrp(base_addr)
            if not hrp:
                hrp = None
            try:
                # Post-SegWit releases derive aux payouts only from the chain
                # HRP. The session-level base address stays untouched unless
                # the username resolves through the V2 mining-key path.
                resolved = resolve_payout_address(username, None, segwit_hrp=hrp)
            except Exception:
                resolved = None
            if not isinstance(resolved, dict):
                continue
            if resolved.get('kind') != 'derived_v2':
                continue
            derived = resolved.get('address')
            if not derived:
                continue
            base_addresses[chain_idx] = str(derived)
            resolved_any = True

        if resolved_any:
            logger.debug("Resolved per-solver aux payout set for %r", username)
        return tuple(base_addresses)

    @staticmethod
    def _cache_key_for_payouts(payout_addresses):
        return '|'.join(addr or '' for addr in payout_addresses)

    @staticmethod
    def _is_stale_aux_submission_error(exc):
        # 15.21 change:
        # Treat normal aux-chain roll-forward/orphan cases as benign.
        # Older behavior could spam retries / error logs for shares that
        # were simply orphaned because the aux chain advanced between
        # template fetch and submitauxblock. We now detect those stale
        # signatures explicitly so they refresh quietly and do not mark
        # the aux daemon unhealthy.
        """Recognise the "aux chain moved on, share is orphaned" condition.

        In merge-mining a parent block carries an `aux_hash` reference to
        the aux chain's tip-at-the-time. Between us pulling that template
        (`getauxblock` / `createauxblock`) and submitting our parent solve
        (`submitauxblock`), the aux chain itself can advance — someone
        else (or even our own previous submission) extends it. The aux
        daemon then has no record of the older `aux_hash`, since it has
        rolled forward, and rejects the submission. The share is orphaned
        AT THE AUX LAYER — perfectly normal and benign in a merge-mining
        pool with multiple aux chains and asymmetric block intervals.

        Different aux daemons signal this with different (code, message)
        shapes:

        - `-8 "block hash unknown"`      — namecoin/blakecoin classic
        - `-8 "block-not-found"`         — newer bitcoin core descendants
        - `-8 "hash not found"`          — some forks
        - `-8 "unknown block"`           — vanilla wording
        - `-32603` internal-error wrapper around any of the above
        - `-1`     generic JSON-RPC server-error wrapper

        We accept all of them as "stale, refresh, move on", log INFO,
        DON'T retry, and DON'T mark the chain unhealthy. The ONLY thing
        we want to mark unhealthy here is a real backend fault — TCP
        refused, parse error, daemon crash. Those leave a different
        signature (httplib/socket exceptions, code -32099 from our own
        retry layer).
        """
        if exc is None:
            return False
        code = getattr(exc, '_code', None)
        message = str(getattr(exc, '_message', '') or exc).lower()
        # Explicit allowlist only — `'block' in message` was too loose
        # and silently swallowed real bugs like "Invalid hex string for
        # block proof". Stale events have one of these specific shapes.
        stale_keywords = (
            'block hash unknown',
            'block-not-found', 'block not found',
            'hash not found',
            'unknown block',
            'not in main chain',
            'not on best chain',
            'inconclusive',
            'stale',
            'orphan',
        )
        is_stale_message = any(k in message for k in stale_keywords)
        # `-8` (RPC_INVALID_PARAMETER) is the canonical signal in
        # bitcoin-core derivatives, but we still require an explicit
        # stale message — `-8` ALSO fires for malformed proofs/hex.
        if code == -8 and is_stale_message:
            return True
        # `-1` / `-32603` are generic JSON-RPC server-error / internal
        # wrappers some forks return; only stale when the message
        # itself indicates the rolled-forward case.
        if code in (-1, -32603) and is_stale_message:
            return True
        return False
    
    def status_report_process(self):
        """Log periodic status report of all connections"""
        reactor.callLater(300, self.status_report_process)  # Every 5 minutes
        self._log_status_report()
    
    def _log_status_report(self):
        """Log connection statistics for all chains"""
        now = time()
        
        # Report parent connections
        for i, p in enumerate(self.parent):
            idle_time = now - p._last_used if p._last_used > 0 else 0
            logger.info("Status: Parent[%d] %s - reqs: %d, failures: %d, idle: %.0fs", 
                        i, p._netloc, p._total_requests, p._consecutive_failures, idle_time)
        
        # Report aux chain connections
        for i, aux in enumerate(self.auxs):
            idle_time = now - aux._last_used if aux._last_used > 0 else 0
            chain_id = self.chain_ids[i] if i < len(self.chain_ids) else 'N/A'
            health_status = "UP" if self._is_chain_healthy(i) else "DOWN"
            logger.info("Status: %s (ID:%s) %s - reqs: %d, failures: %d, idle: %.0fs [%s]", 
                        get_chain_name(i), chain_id, aux._netloc, aux._total_requests, aux._consecutive_failures, idle_time, health_status)
    
    def _is_chain_healthy(self, chain_idx):
        """Check if aux chain is currently healthy"""
        if chain_idx not in self.chain_health:
            # Unknown status - assume healthy initially
            return True
        health = self.chain_health[chain_idx]
        # Consider healthy if: marked healthy AND not too many consecutive failures
        return health.get('healthy', True) and health.get('failures', 0) < 3
    
    def _mark_chain_healthy(self, chain_idx):
        """Mark chain as healthy"""
        if chain_idx not in self.chain_health:
            self.chain_health[chain_idx] = {}
        self.chain_health[chain_idx]['healthy'] = True
        self.chain_health[chain_idx]['last_success'] = time()
        self.chain_health[chain_idx]['failures'] = 0
        # Log recovery if it was previously down
        if not self.chain_health[chain_idx].get('was_reported_healthy', False):
            logger.info("%s (ID:%s) is now HEALTHY", get_chain_name(chain_idx), 
                       self.chain_ids[chain_idx] if chain_idx < len(self.chain_ids) else 'N/A')
            self.chain_health[chain_idx]['was_reported_healthy'] = True
    
    def _mark_chain_unhealthy(self, chain_idx):
        """Mark chain as unhealthy"""
        if chain_idx not in self.chain_health:
            self.chain_health[chain_idx] = {}
        was_healthy = self.chain_health[chain_idx].get('healthy', True)
        self.chain_health[chain_idx]['healthy'] = False
        self.chain_health[chain_idx]['failures'] = self.chain_health[chain_idx].get('failures', 0) + 1
        self.chain_health[chain_idx]['last_failure'] = time()
        self.chain_health[chain_idx]['was_reported_healthy'] = False
        if was_healthy:
            logger.warning("%s (ID:%s) marked as UNHEALTHY after failure", get_chain_name(chain_idx),
                          self.chain_ids[chain_idx] if chain_idx < len(self.chain_ids) else 'N/A')
    
    def _get_healthy_chains(self):
        """Get list of indices of healthy chains"""
        return [i for i in range(len(self.auxs)) if self._is_chain_healthy(i)]
    
    def _count_healthy_chains(self):
        """Count number of healthy chains"""
        return len(self._get_healthy_chains())
    
    def merkle_branch(self, chain_index, merkle_tree):
        """Calculate merkle branch for a chain"""
        step = self.merkle_size
        i1 = chain_index
        j = 0
        branch = []
        while step > 1:
            i = min(i1^1, step-1)
            branch.append(merkle_tree[i + j])
            i1 = i1 >> 1
            j += step
            step = (step + 1) // 2
        return branch

    def calc_merkle_index_for(self, chain_id, nonce):
        """Calculate the merkle tree index for a chain ID and aux nonce."""
        rand = nonce
        rand = (rand * 1103515245 + 12345) & 0xffffffff
        rand += chain_id
        rand = (rand * 1103515245 + 12345) & 0xffffffff
        return rand % self.merkle_size

    def calc_merkle_index(self, chain, nonce=0):
        """Calculate merkle tree index for a chain."""
        chain_id = self.chain_ids[chain]
        return self.calc_merkle_index_for(chain_id, nonce)

    def choose_merkle_nonce(self, chain_ids):
        """Pick a nonce that yields unique merkle slots for the active chain IDs."""
        active_chain_ids = [chain_id for chain_id in chain_ids if chain_id is not None]
        if len(active_chain_ids) <= 1:
            return 0
        for nonce in range(0, 1 << 16):
            used = set()
            collision = False
            for chain_id in active_chain_ids:
                idx = self.calc_merkle_index_for(chain_id, nonce)
                if idx in used:
                    collision = True
                    break
                used.add(idx)
            if not collision:
                return nonce
        raise ValueError("Unable to find collision-free aux merkle nonce")

    @staticmethod
    def encode_le32_hex(value):
        return value.to_bytes(4, 'little').hex()

    @defer.inlineCallbacks
    def _build_aux_template(self, payout_addresses=None, propagate_parent=False):
        """Build an aux template set for one payout-address vector.

        The legacy live path keeps using one shared session vector via
        setworkaux. The per-solver path calls this on demand and embeds the
        returned coinbase aux blob directly into miner-specific parent jobs.
        """
        payout_addresses = self._normalize_payout_addresses(payout_addresses)
        # Create merkle leaves with arbitrary initial value
        merkle_leaves = [('0' * 62) + ("%02x" % x) for x in range(self.merkle_size)]
        aux_block_hashes = {}

        # Ask each aux chain for a block
        healthy_count = 0
        for chain in range(len(self.auxs)):
            try:
                payout_address = payout_addresses[chain]
                if payout_address is not None:
                    logger.debug("%s: requesting createauxblock for payout address %s", get_chain_name(chain), payout_address)
                    aux_block = yield self.auxs[chain].rpc_createauxblock(payout_address)
                else:
                    aux_block = yield self.auxs[chain].rpc_getauxblock()
                if not aux_block or 'hash' not in aux_block:
                    logger.warning("%s: Invalid aux_block response", get_chain_name(chain))
                    self._mark_chain_unhealthy(chain)
                    continue
                aux_block_hash = aux_block['hash']
                self.chain_ids[chain] = aux_block.get('chainid', chain)
                aux_block_hashes[chain] = aux_block_hash
                self.aux_targets[chain] = reverse_chunks(aux_block['target'], 2)
                logger.debug("%s: Got aux block hash=%s", get_chain_name(chain), aux_block_hash[:16])
                self._mark_chain_healthy(chain)
                healthy_count += 1
            except Exception as e:
                if self._is_stale_aux_submission_error(e):
                    # Daemon answered (just with a stale-hash response),
                    # so it's responsive — mark healthy in case a prior
                    # transient had taken it out of rotation.
                    logger.info(
                        "%s: aux template fetch saw stale state (%s); refreshing",
                        get_chain_name(chain),
                        e,
                    )
                    self._mark_chain_healthy(chain)
                    continue
                logger.error("%s: Failed to get aux block: %s", get_chain_name(chain), e)
                self._mark_chain_unhealthy(chain)
                continue
        
        # Report health status
        total_chains = len(self.auxs)
        if healthy_count < total_chains:
            logger.warning("Only %d/%d aux chains are healthy", healthy_count, total_chains)
        else:
            logger.debug("All %d aux chains are healthy", total_chains)

        merkle_nonce = self.choose_merkle_nonce([self.chain_ids[chain] if chain in aux_block_hashes else None for chain in range(len(self.auxs))])
        chain_indices = {}
        for chain, aux_block_hash in aux_block_hashes.items():
            chain_merkle_index = self.calc_merkle_index(chain, merkle_nonce)
            chain_indices[chain] = chain_merkle_index
            merkle_leaves[chain_merkle_index] = aux_block_hash

        # Create merkle tree
        if len(merkle_leaves) > 1:
            # Python 3 fix: ensure we're working with strings properly
            mt = MerkleTree([a2b_hex(s.encode())[::-1] for s in merkle_leaves], detailed=True)
            merkle_tree = [b2a_hex(s[::-1]).decode() for s in mt.detail]
        else:
            merkle_tree = merkle_leaves
        merkle_root = merkle_tree[-1]

        mmaux = merkle_root + self.encode_le32_hex(self.merkle_size) + self.encode_le32_hex(merkle_nonce)
        if merkle_root not in self.merkle_trees:
            if propagate_parent:
                # The shared session path still updates the parent's global MM
                # blob. Per-solver jobs use the returned coinbase aux directly
                # instead, so they don't have to race over one global root.
                coinbase_aux = 'fabe6d6d' + mmaux
                for p in self.parent:
                    try:
                        logger.debug("Sending setworkaux to parent %s", p._netloc)
                        p.rpc_setworkaux('MM', coinbase_aux)
                        logger.debug("Parent %s setworkaux successful", p._netloc)
                    except Exception as e:
                        logger.error("Parent %s setworkaux failed: %s", p._netloc, e)
                        logger.error("Failed to update parent chain with new merkle root - aux mining data may be stale")

            self.merkle_trees[merkle_root] = {
                'tree': merkle_tree,
                'nonce': merkle_nonce,
                'chain_indices': chain_indices,
                'aux_hashes': aux_block_hashes,
                'payout_addresses': list(payout_addresses),
            }
            self.merkle_tree_queue.append(merkle_root)
            if len(self.merkle_tree_queue) > MERKLE_TREES_TO_KEEP:
                # Forget one tree
                old_root = self.merkle_tree_queue.pop(0)
                del self.merkle_trees[old_root]

        result = {
            'aux': mmaux,
            'coinbaseaux': 'fabe6d6d' + mmaux,
            'merkle_root': merkle_root,
            'payout_addresses': list(payout_addresses),
        }
        if self.rewrite_target:
            result['aux_target'] = self.rewrite_target
        else:
            targets = [t for t in self.aux_targets if t]
            if targets:
                targets.sort()
                result['aux_target'] = reverse_chunks(targets[-1], 2)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def update_auxs(self):
        """Update merkle tree from aux chains - only include healthy chains"""
        # Shared-session refreshes should also invalidate per-solver aux blobs so
        # miners stop reusing roots that predate fresh child mempool contents.
        self.per_solver_cache.clear()
        result = yield self._build_aux_template(propagate_parent=True)
        logger.debug("Merkle tree updated, root=%s...", result['merkle_root'][:16])

    def update_aux_process(self):
        """Periodic aux update"""
        reactor.callLater(AUX_UPDATE_INTERVAL, self.update_aux_process)
        self.update_auxs()

    @defer.inlineCallbacks
    def rpc_getaux(self, data=None):
        """Get aux chain merkle root and aux target"""
        try:
            if data not in (None, ''):
                payout_addresses = self._resolve_selector_payout_addresses(data)
                cache_key = self._cache_key_for_payouts(payout_addresses)
                cached = self.per_solver_cache.get(cache_key)
                if cached and time() <= cached['time'] + AUX_SOLVER_CACHE_TTL:
                    defer.returnValue(cached['result'])
                    return
                result = yield self._build_aux_template(payout_addresses=payout_addresses, propagate_parent=False)
                self.per_solver_cache[cache_key] = {'time': time(), 'result': result}
                defer.returnValue(result)
                return

            # Check if merkle tree queue has data
            if not self.merkle_tree_queue:
                logger.warning("rpc_getaux: Merkle tree queue is empty")
                defer.returnValue({'aux': '00' * 40 + ("%02x000000" % self.merkle_size) + "00000000", 'error': 'aux chains not ready'})
                return

            merkle_root = self.merkle_tree_queue[-1]
            merkle_meta = self.merkle_trees[merkle_root]
            merkle_nonce = merkle_meta.get('nonce', 0)
            aux = merkle_root + self.encode_le32_hex(self.merkle_size) + self.encode_le32_hex(merkle_nonce)
            result = {'aux': aux, 'coinbaseaux': 'fabe6d6d' + aux}

            if self.rewrite_target:
                result['aux_target'] = self.rewrite_target
            else:
                targets = [t for t in self.aux_targets if t]
                if targets:
                    targets.sort()
                    result['aux_target'] = reverse_chunks(targets[-1], 2)

            defer.returnValue(result)
        except Exception:
            logger.error(traceback.format_exc())
            raise

    @defer.inlineCallbacks
    def rpc_gotwork(self, solution):
        """Handle work submission from pool"""
        try:
            # Submit work upstream
            any_solved = False
            stale_aux_submission = False
            aux_solved = []
            accepted_details = []

            parent_hash = solution['hash']
            parent_valid = bool(solution.get('parent_valid'))
            parent_status = solution.get('parent_status') or ('parent-accepted' if parent_valid else 'parent-rejected')
            blkhdr = solution['header']
            coinbaseMerkle = solution['coinbaseMrkl']
            pos = coinbaseMerkle.find('fabe6d6d')
            if pos < 0:
                logger.error("failed to find aux in coinbase")
                defer.returnValue(False)
                return
            pos += 8  # Skip the merge-mining magic marker itself.
            slnaux = coinbaseMerkle[pos:pos+80]

            merkle_root = slnaux[:-16]  # strip off size and nonce
            
            # Handle unknown merkle root - create a placeholder
            if merkle_root not in self.merkle_trees:
                logger.warning("Unknown merkle root %s - adding placeholder", merkle_root[:16])
                self.merkle_trees[merkle_root] = {
                    'tree': [merkle_root],
                    'nonce': 0,
                    'chain_indices': {},
                    'aux_hashes': {},
                }
                self.merkle_tree_queue.append(merkle_root)
                if len(self.merkle_tree_queue) > MERKLE_TREES_TO_KEEP:
                    old_root = self.merkle_tree_queue.pop(0)
                    if old_root in self.merkle_trees:
                        del self.merkle_trees[old_root]

            merkle_meta = self.merkle_trees[merkle_root]
            merkle_tree = merkle_meta['tree']
            merkle_nonce = merkle_meta.get('nonce', 0)
            merkle_payout_addresses = tuple(merkle_meta.get('payout_addresses') or ())

            # Submit to each aux chain
            for chain in range(len(self.auxs)):
                chain_merkle_index = merkle_meta.get('chain_indices', {}).get(chain)
                if chain_merkle_index is None:
                    chain_merkle_index = self.calc_merkle_index(chain, merkle_nonce)
                aux_solved.append(False)
                
                # Skip known-unhealthy chains to avoid wasted submissions
                if not self._is_chain_healthy(chain):
                    chain_id = self.chain_ids[chain] if chain < len(self.chain_ids) else 'N/A'
                    logger.debug("%s (ID:%s) is unhealthy - skipping submission", get_chain_name(chain), chain_id)
                    continue
                
                if chain_merkle_index is not None:
                    branch = self.merkle_branch(chain_merkle_index, merkle_tree)
                    auxpow = coinbaseMerkle + '%02x' % (len(branch),)
                    for mb in branch:
                        auxpow += b2a_hex(a2b_hex(mb.encode())[::-1]).decode()
                    auxpow += self.encode_le32_hex(chain_merkle_index) + blkhdr
                    aux_hash = merkle_meta.get('aux_hashes', {}).get(chain)
                    if aux_hash is None:
                        aux_hash = merkle_tree[chain_merkle_index] if chain_merkle_index < len(merkle_tree) else merkle_root

                    # Single submit attempt — `Proxy.callRemote` already
                    # retries 3x with exponential backoff for transient
                    # connection errors and trips a circuit breaker. A
                    # second retry layer here was duplicative (3x3 = 9
                    # daemon hits per submitauxblock for connection
                    # faults) and added no value: stale errors break out
                    # immediately, malformed-proof bugs return identical
                    # answers on retry. Trust the lower layer; surface a
                    # single decisive outcome per submit.
                    logger.info("%s: aux_hash=%s merkle_index=%s",
                                get_chain_name(chain), aux_hash, chain_merkle_index)
                    if chain < len(merkle_payout_addresses):
                        payout_address = merkle_payout_addresses[chain]
                    else:
                        payout_address = self._get_aux_payout_address(chain)
                    try:
                        if payout_address is not None:
                            result = yield self.auxs[chain].rpc_submitauxblock(aux_hash, auxpow)
                        else:
                            result = yield self.auxs[chain].rpc_getauxblock(aux_hash, auxpow)
                        aux_solved[-1] = result
                        if result:
                            logger.info("%s: Block accepted!", get_chain_name(chain))
                            accepted_details.append((yield fetch_chain_acceptance_details(self.auxs[chain], chain, payout_address=payout_address)))
                            # Successful submit confirms chain is healthy.
                            self._mark_chain_healthy(chain)
                        else:
                            # Some daemons signal "aux chain moved past
                            # this hash" by returning False instead of
                            # raising -8. Same operational meaning as the
                            # exception path: share orphaned at the aux
                            # layer, no retry, daemon answered so it's
                            # responsive and we can mark it healthy.
                            logger.info(
                                "%s: aux daemon returned not-accepted for hash %s "
                                "(likely orphan or already submitted); refreshing template",
                                get_chain_name(chain), aux_hash,
                            )
                            stale_aux_submission = True
                            self._mark_chain_healthy(chain)
                        any_solved = any_solved or result
                    except Exception as e:
                        if self._is_stale_aux_submission_error(e):
                            # Aux chain moved past this share — the share
                            # is orphaned at the aux layer. Log INFO,
                            # don't retry, mark chain healthy (the daemon
                            # just answered, just with a stale-hash
                            # response — proves it's responsive).
                            logger.info(
                                "%s: aux chain moved past hash %s (share orphaned at aux layer); refreshing template",
                                get_chain_name(chain), aux_hash,
                            )
                            stale_aux_submission = True
                            self._mark_chain_healthy(chain)
                        else:
                            # Real backend fault — `callRemote` already
                            # exhausted its 3x retry. Mark unhealthy so
                            # we skip this chain on subsequent solves
                            # until the next template-fetch round
                            # confirms it's back.
                            logger.error(
                                "%s: submission failed: %s — marking chain unhealthy",
                                get_chain_name(chain), e,
                            )
                            self._mark_chain_unhealthy(chain)

            accepted_labels = [get_chain_name(i) for i, solve in enumerate(aux_solved) if solve]
            solve_flags = ",".join(["1" if solve else "0" for solve in aux_solved])
            solve_ts = datetime.utcnow().isoformat()
            logger.info("%s,solve,%s,%s,%s",
                solve_ts,
                "=",
                solve_flags,
                parent_hash[:16])
            if any_solved or parent_valid:
                logger.info("%s,solve_status,%s,%s,%s",
                    solve_ts,
                    parent_status,
                    solve_flags,
                    parent_hash)
            if accepted_details:
                logger.info(
                    "%s,solve_detail,%s,%s,%s",
                    solve_ts,
                    parent_status,
                    parent_hash,
                    json.dumps({'accepted': accepted_details}, sort_keys=True, separators=(',', ':')),
                )
            if (not parent_valid) and accepted_labels:
                logger.info(
                    "Parent rejected / child accepted: parent_hash=%s chains=%s",
                    parent_hash[:16],
                    ",".join(accepted_labels),
                )

            if any_solved or parent_valid or stale_aux_submission:
                self.per_solver_cache.clear()
                self.update_auxs()
            
            defer.returnValue(any_solved)
        except Exception:
            logger.error(traceback.format_exc())
            raise

def main(args):
    parent = list(map(Proxy, args.parent_url))
    aux_urls = args.aux_urls or ['http://un:pw@127.0.0.1:8342/']
    auxs = [Proxy(url) for url in aux_urls]
    aux_payout_addresses = list(args.aux_payout_addresses or [])

    if aux_payout_addresses and len(aux_payout_addresses) != len(aux_urls):
        raise ValueError('The number of aux payout addresses must match the number of aux URLs')
    
    if args.merkle_size is None:
        for i in range(8):
            if (1 << i) > len(aux_urls):
                args.merkle_size = i
                logger.debug('Auto-detected merkle size = %d', i)
                break

    if len(aux_urls) > args.merkle_size:
        raise ValueError('The merkle size must be at least as large as the number of aux chains')

    if args.merkle_size > 1 and not have_merkletree:
        raise ValueError('Missing merkletree module. Only a single subchain will work.')

    if args.pidfile:
        with open(args.pidfile, 'w') as pidfile:
            pidfile.write(str(os.getpid()))

    listener = Listener(
        parent,
        auxs,
        args.merkle_size,
        args.rewrite_target,
        aux_payout_addresses=aux_payout_addresses,
        per_solver_aux_payouts=args.per_solver_aux_payouts,
    )
    
    # Bind to IPv4 only - change '0.0.0.0' to listen on all interfaces
    from twisted.internet import endpoints
    endpoint = endpoints.TCP4ServerEndpoint(reactor, args.worker_port, interface='127.0.0.1')
    endpoint.listen(server.Site(listener))

def run():
    parser = argparse.ArgumentParser(description='merge-mine-proxy (version %s)' % (__version__,))
    parser.add_argument('--version', action='version', version=__version__)
    
    worker_group = parser.add_argument_group('worker interface')
    worker_group.add_argument('-w', '--worker-port', metavar='PORT',
        help='Listen on PORT for RPC connections (default: 9992)',
        type=int, action='store', default=9992, dest='worker_port')

    parent_group = parser.add_argument_group('parent chain interface')
    parent_group.add_argument('-p', '--parent-url', metavar='PARENT_URL',
        help='Parent chain RPC URL (default: http://un:pw@127.0.0.1:8332/)',
        type=str, action='store', nargs='+', dest='parent_url')

    aux_group = parser.add_argument_group('aux chain interface(s)')
    aux_group.add_argument('-x', '--aux-url', metavar='AUX_URL',
        help='Aux chain RPC URL (default: http://un:pw@127.0.0.1:8342/)',
        type=str, action='append', default=[], dest='aux_urls')
    aux_group.add_argument('-a', '--aux-payout-address', metavar='ADDRESS',
        help='Explicit aux payout address matched by order with --aux-url; enables createauxblock/submitauxblock for that chain',
        type=str, action='append', default=[], dest='aux_payout_addresses')
    aux_group.add_argument('--per-solver-aux-payouts',
        help='Rewrite aux payout addresses per miner identity instead of paying the configured pool addresses',
        action='store_true', default=False, dest='per_solver_aux_payouts')
    aux_group.add_argument('-s', '--merkle-size', metavar='SIZE',
        help='Merkle tree entries (must be power of 2)',
        type=int, action='store', default=None, dest='merkle_size')

    parser.add_argument('-r', '--rewrite-target', 
        help='Rewrite target difficulty to 32',
        action='store_const', const=32, default=False, dest='rewrite_target')
    parser.add_argument('-R', '--rewrite-target-100', 
        help='Rewrite target difficulty to 100',
        action='store_const', const=100, default=False, dest='rewrite_target')
    parser.add_argument('-i', '--pidfile', metavar='PID', 
        type=str, action='store', default=None, dest='pidfile')
    parser.add_argument('-l', '--logfile', metavar='LOG', 
        type=str, action='store', default=None, dest='logfile')

    args = parser.parse_args()

    if args.logfile:
        file_handler = logging.FileHandler(args.logfile)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    reactor.callWhenRunning(main, args)
    reactor.run()

if __name__ == "__main__":
    run()
