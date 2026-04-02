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

from twisted.internet import defer, reactor, threads
from twisted.web import server, resource
from twisted.internet.error import ConnectionRefusedError
import twisted.internet.error
from urllib.parse import urlsplit
import http.client as httplib
import _thread

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

__version__ = '0.2.3-py3'

AUX_UPDATE_INTERVAL = 5
MERKLE_TREES_TO_KEEP = 240

# Chain name mapping for logging (matches database naming)
CHAIN_NAMES = {0: 'MM', 1: 'MM1', 2: 'MM3', 3: 'MM4', 4: 'MM5'}

def get_chain_name(chain_idx):
    """Get display name for chain index"""
    return CHAIN_NAMES.get(chain_idx, 'MM%d' % chain_idx)

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
        # Circuit breaker check
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Circuit breaker open for %s - too many failures", self._url)
            raise Error(-32099, 'Circuit breaker open - too many connection failures', self._url)
        
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
    def __init__(self, parent, auxs, merkle_size, rewrite_target):
        Server.__init__(self)
        self.parent = parent
        self.auxs = auxs
        self.chain_ids = [None for i in auxs]
        self.aux_targets = [None for i in auxs]
        self.chain_health = {}  # chain_idx -> {'healthy': bool, 'last_success': time, 'failures': count}
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

    def calc_merkle_index(self, chain):
        """Calculate merkle tree index for a chain"""
        chain_id = self.chain_ids[chain]
        rand = 0  # nonce
        rand = (rand * 1103515245 + 12345) & 0xffffffff
        rand += chain_id
        rand = (rand * 1103515245 + 12345) & 0xffffffff
        return rand % self.merkle_size

    @defer.inlineCallbacks
    def update_auxs(self):
        """Update merkle tree from aux chains - only include healthy chains"""
        # Create merkle leaves with arbitrary initial value
        merkle_leaves = [('0' * 62) + ("%02x" % x) for x in range(self.merkle_size)]

        # Ask each aux chain for a block
        healthy_count = 0
        for chain in range(len(self.auxs)):
            try:
                aux_block = yield self.auxs[chain].rpc_getauxblock()
                if not aux_block or 'hash' not in aux_block:
                    logger.warning("%s: Invalid aux_block response", get_chain_name(chain))
                    self._mark_chain_unhealthy(chain)
                    continue
                aux_block_hash = aux_block['hash']
                self.chain_ids[chain] = aux_block.get('chainid', chain)
                chain_merkle_index = self.calc_merkle_index(chain)
                merkle_leaves[chain_merkle_index] = aux_block_hash
                self.aux_targets[chain] = reverse_chunks(aux_block['target'], 2)
                logger.debug("%s: Got aux block hash=%s", get_chain_name(chain), aux_block_hash[:16])
                self._mark_chain_healthy(chain)
                healthy_count += 1
            except Exception as e:
                logger.error("%s: Failed to get aux block: %s", get_chain_name(chain), e)
                self._mark_chain_unhealthy(chain)
                continue
        
        # Report health status
        total_chains = len(self.auxs)
        if healthy_count < total_chains:
            logger.warning("Only %d/%d aux chains are healthy", healthy_count, total_chains)
        else:
            logger.debug("All %d aux chains are healthy", total_chains)

        # Create merkle tree
        if len(merkle_leaves) > 1:
            # Python 3 fix: ensure we're working with strings properly
            mt = MerkleTree([a2b_hex(s.encode())[::-1] for s in merkle_leaves], detailed=True)
            merkle_tree = [b2a_hex(s[::-1]).decode() for s in mt.detail]
        else:
            merkle_tree = merkle_leaves
        merkle_root = merkle_tree[-1]

        if merkle_root not in self.merkle_trees:
            # Tell bitcoind the new merkle root
            MMAux = merkle_root + ("%02x000000" % self.merkle_size) + "00000000"
            MMAux = 'fabe6d6d' + MMAux
            for p in self.parent:
                try:
                    logger.debug("Sending setworkaux to parent %s", p._netloc)
                    p.rpc_setworkaux('MM', MMAux)
                    logger.debug("Parent %s setworkaux successful", p._netloc)
                except Exception as e:
                    logger.error("Parent %s setworkaux failed: %s", p._netloc, e)
                    logger.error("Failed to update parent chain with new merkle root - aux mining data may be stale")

            # Remember new tree
            self.merkle_trees[merkle_root] = merkle_tree
            self.merkle_tree_queue.append(merkle_root)
            if len(self.merkle_tree_queue) > MERKLE_TREES_TO_KEEP:
                # Forget one tree
                old_root = self.merkle_tree_queue.pop(0)
                del self.merkle_trees[old_root]
        
        logger.debug("Merkle tree updated, root=%s...", merkle_root[:16])

    def update_aux_process(self):
        """Periodic aux update"""
        reactor.callLater(AUX_UPDATE_INTERVAL, self.update_aux_process)
        self.update_auxs()

    def rpc_getaux(self, data=None):
        """Get aux chain merkle root and aux target"""
        try:
            # Check if merkle tree queue has data
            if not self.merkle_tree_queue:
                logger.warning("rpc_getaux: Merkle tree queue is empty")
                # Return dummy/error response
                return {'aux': '00' * 40 + ("%02x000000" % self.merkle_size) + "00000000", 'error': 'aux chains not ready'}
            
            # Get aux based on the latest tree
            merkle_root = self.merkle_tree_queue[-1]
            # nonce = 0, one byte merkle size
            aux = merkle_root + ("%02x000000" % self.merkle_size) + "00000000"
            result = {'aux': aux}

            if self.rewrite_target:
                result['aux_target'] = self.rewrite_target
            else:
                # Find highest target
                targets = list(self.aux_targets)
                targets.sort()
                result['aux_target'] = reverse_chunks(targets[-1], 2)
            
            return result
        except Exception:
            logger.error(traceback.format_exc())
            raise

    @defer.inlineCallbacks
    def rpc_gotwork(self, solution):
        """Handle work submission from pool"""
        try:
            # Submit work upstream
            any_solved = False
            aux_solved = []

            parent_hash = solution['hash']
            blkhdr = solution['header']
            coinbaseMerkle = solution['coinbaseMrkl']
            pos = coinbaseMerkle.find('fabe6d6d')
            if pos < 0:
                logger.error("failed to find aux in coinbase")
                defer.returnValue(False)
                return
            pos += 8  # Skip 'fabe6d6d'
            slnaux = coinbaseMerkle[pos:pos+80]

            merkle_root = slnaux[:-16]  # strip off size and nonce
            
            # Handle unknown merkle root - create a placeholder
            if merkle_root not in self.merkle_trees:
                logger.warning("Unknown merkle root %s - adding placeholder", merkle_root[:16])
                self.merkle_trees[merkle_root] = [merkle_root]
                self.merkle_tree_queue.append(merkle_root)
                if len(self.merkle_tree_queue) > MERKLE_TREES_TO_KEEP:
                    old_root = self.merkle_tree_queue.pop(0)
                    if old_root in self.merkle_trees:
                        del self.merkle_trees[old_root]

            merkle_tree = self.merkle_trees[merkle_root]

            # Submit to each aux chain
            for chain in range(len(self.auxs)):
                chain_merkle_index = self.calc_merkle_index(chain)
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
                    auxpow += ('%02x000000' % (chain_merkle_index,)) + blkhdr
                    aux_hash = merkle_tree[chain_merkle_index] if chain_merkle_index < len(merkle_tree) else merkle_root
                    
                    # Retry logic for block submission - critical operation
                    max_submit_retries = 3
                    for submit_attempt in range(max_submit_retries):
                        try:
                            logger.debug("%s: Submitting block (attempt %d/%d)", 
                                         get_chain_name(chain), submit_attempt + 1, max_submit_retries)
                            result = yield self.auxs[chain].rpc_getauxblock(aux_hash, auxpow)
                            aux_solved[-1] = result
                            if result:
                                logger.info("%s: Block accepted!", get_chain_name(chain))
                                self._mark_chain_healthy(chain)  # Success confirms chain is healthy
                            else:
                                logger.debug("%s: Block not accepted (likely orphan or already submitted)", get_chain_name(chain))
                            any_solved = any_solved or result
                            break  # Success - exit retry loop
                        except Exception as e:
                            logger.error("%s submission failed (attempt %d/%d): %s", 
                                         get_chain_name(chain), submit_attempt + 1, max_submit_retries, e)
                            if submit_attempt < max_submit_retries - 1:
                                # Wait before retry (exponential backoff)
                                wait_time = min(0.5 * (2 ** submit_attempt), 3.0)
                                logger.info("%s: Retrying in %.1fs...", get_chain_name(chain), wait_time)
                                yield defer.sleep(wait_time)
                            else:
                                logger.error("%s: All submission attempts failed - marking unhealthy", get_chain_name(chain))
                                self._mark_chain_unhealthy(chain)

            logger.info("%s,solve,%s,%s,%s", 
                datetime.utcnow().isoformat(),
                "=",
                ",".join(["1" if solve else "0" for solve in aux_solved]),
                parent_hash[:16])
            
            if any_solved:
                self.update_auxs()
            
            defer.returnValue(any_solved)
        except Exception:
            logger.error(traceback.format_exc())
            raise

def main(args):
    parent = list(map(Proxy, args.parent_url))
    aux_urls = args.aux_urls or ['http://un:pw@127.0.0.1:8342/']
    auxs = [Proxy(url) for url in aux_urls]
    
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

    listener = Listener(parent, auxs, args.merkle_size, args.rewrite_target)
    
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
