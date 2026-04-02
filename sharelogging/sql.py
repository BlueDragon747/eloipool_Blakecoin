# Eloipool - Python Bitcoin pool server
# Copyright (C) 2011-2012  Luke Dashjr <luke-jr+eloipool@utopios.org>
# Portions written by BlueDragon747 for the Blakecoin Project
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Shared Write Buffer with Batched Inserts
# This implementation uses a single MySQL connection and writer thread
# for all chains, reducing connection overhead and batching inserts
# for significantly better performance.

import logging
from queue import Queue, Full
import threading
import traceback
from time import time, sleep
from util import shareLogFormatter

_logger = logging.getLogger('sharelogging.sql')

# Global shared state across all sql instances
_shared_queue = None
_writer_thread = None
_writer_lock = threading.Lock()
_instance_count = 0
_instance_lock = threading.Lock()
_dbopts_storage = {}  # Store dbopts by chain_id for shared writer access
_dbopts_lock = threading.Lock()

class sql:
	_psf = {
		'qmark': '?',
		'format': '%s',
		'pyformat': '%s',
	}
	
	# Configuration for batching and queue management
	BATCH_SIZE = 10  # Number of shares per batch insert - balanced for performance and timestamp accuracy
	QUEUE_MAXSIZE = 10000  # Max shares in queue before blocking/dropping
	FLUSH_INTERVAL = 0.5  # Max seconds to wait before flushing partial batch
	RECONNECT_DELAY = 1.0  # Seconds to wait before reconnecting
	MAX_RECONNECT_ATTEMPTS = 5  # Max reconnection attempts before giving up
	
	def __init__(self, **ka):
		global _shared_queue, _writer_thread, _instance_count
		
		self.opts = ka
		dbe = ka['engine']
		self.chain_id = ka.get('chain_id', 'unknown')  # Identify which chain
		
		if 'statement' not in ka:
			_logger.warning('"statement" not specified for sql logger, but default may vary!')
		
		self.exceptions = []
		self.threadsafe = False  # Force use of shared writer
		
		# Track instance for shutdown
		with _instance_lock:
			_instance_count += 1
			self._instance_num = _instance_count
		
		# Initialize module and statement formatter
		getattr(self, 'setup_%s' % (dbe,))()
		
		# Store dbopts globally so shared writer can access them
		with _dbopts_lock:
			if self.chain_id not in _dbopts_storage:
				_dbopts_storage[self.chain_id] = {
					'dbopts': self.dbopts,
					'mod': self._mod,
					'engine': dbe
				}
				_logger.info('Chain %s: Stored database configuration for shared writer', self.chain_id)
		
		# Initialize shared queue if not exists
		with _writer_lock:
			if _shared_queue is None:
				_shared_queue = Queue(maxsize=self.QUEUE_MAXSIZE)
				_logger.debug('Initialized shared queue (maxsize=%d)', self.QUEUE_MAXSIZE)
		
		self._shared_queue = _shared_queue
		
		# Start shared writer thread if not running
		with _writer_lock:
			if _writer_thread is None or not _writer_thread.is_alive():
				_writer_thread = threading.Thread(
					target=self._shared_writer,
					name='SharedSQLWriter',
					daemon=True
				)
				_writer_thread.start()
				_logger.debug('Started shared writer thread')
		
		# Set the logging function to use shared queue
		self._logShareF = self._queue_to_shared
		_logger.info('Chain %s ready', self.chain_id)
	
	def _queue_to_shared(self, o):
		"""Queue a share to the global shared queue with overflow protection"""
		try:
			# Try non-blocking put first
			self._shared_queue.put_nowait((self.chain_id, o))
		except Full:
			# Queue is full - this is critical
			_logger.error('Chain %s: Shared queue FULL (%d items), share dropped!', 
						  self.chain_id, self._shared_queue.qsize())
			# Optionally: implement blocking with timeout
			# try:
			#     self._shared_queue.put((self.chain_id, o), timeout=0.1)
			# except Full:
			#     _logger.error('Share dropped after timeout')
	
	@staticmethod
	def _shared_writer():
		"""
		Single writer thread that handles all chains.
		Collects shares into batches and performs batched inserts.
		"""
		global _shared_queue, _dbopts_storage
		
		# Collect all unique database configurations
		db_connections = {}
		statements = {}
		
		_logger.debug('Shared writer thread starting')
		
		# Wait for at least one chain's dbopts to be stored
		wait_count = 0
		while not _dbopts_storage and wait_count < 30:
			_logger.debug('Waiting for database configuration... (attempt %d/30)', wait_count + 1)
			sleep(1)
			wait_count += 1
		
		if not _dbopts_storage:
			_logger.error('No database configuration received after 30 seconds, writer shutting down')
			return
		
		_logger.debug('Database configuration received, starting batch processing')
		
		while True:
			batch = []  # [(chain_id, (stmt, params)), ...]
			batch_chains = set()  # Track which chains are in this batch
			batch_start_time = time()
			
			# Collect shares until batch is full or timeout
			while len(batch) < sql.BATCH_SIZE:
				elapsed = time() - batch_start_time
				remaining = sql.FLUSH_INTERVAL - elapsed
				
				if remaining <= 0 and batch:
					# Timeout reached and we have data - flush what we have
					break
				
				try:
					# Wait for data with timeout
					item = _shared_queue.get(timeout=max(0.1, remaining) if batch else None)
					
					if item is None:
						# Shutdown signal
						if batch:
							sql._flush_batch(batch, db_connections, statements)
						_logger.debug('Shared writer shutting down')
						return
					
					chain_id, share_data = item
					batch.append((chain_id, share_data))
					batch_chains.add(chain_id)
					
				except Exception:
					# Timeout or other error - if we have data, flush it
					if batch:
						break
					continue
			
			# Flush the batch
			if batch:
				sql._flush_batch(batch, db_connections, statements)
				#_logger.debug('Flushed batch of %d shares from %d chains', len(batch), len(batch_chains))
	
	@staticmethod
	def _flush_batch(batch, db_connections, statements):
		"""
		Flush a batch of shares to the database.
		Handles batched inserts with fallback to individual inserts on failure.
		"""
		# Group by chain to handle different tables
		chain_batches = {}
		for chain_id, (stmt, params) in batch:
			if chain_id not in chain_batches:
				chain_batches[chain_id] = []
			chain_batches[chain_id].append((stmt, params))
		
		# Process each chain's batch
		for chain_id, chain_batch in chain_batches.items():
			sql._flush_chain_batch(chain_id, chain_batch, db_connections, statements)
	
	@staticmethod
	def _flush_chain_batch(chain_id, chain_batch, db_connections, statements):
		"""
		Flush a batch for a single chain with reconnection logic.
		"""
		if not chain_batch:
			return
		
		# Get or create connection for this chain
		if chain_id not in db_connections:
			_logger.info('Initializing database connection for chain %s', chain_id)
			statements[chain_id] = chain_batch[0][0]  # Save the statement template
			# Connection will be created on first use below
			
		stmt_template = statements.get(chain_id)
		if not stmt_template:
			_logger.error('No statement template for chain %s', chain_id)
			return
		
		db = db_connections.get(chain_id)
		attempts = 0
		
		while attempts < sql.MAX_RECONNECT_ATTEMPTS:
			try:
				if db is None:
					# Create new connection using stored dbopts
					_logger.info('Creating new DB connection for chain %s', chain_id)
					db = sql._reconnect(chain_id)
					db_connections[chain_id] = db
					_logger.info('Successfully connected for chain %s', chain_id)
				
				# Try batched insert
				dbc = db.cursor()
				params_list = [item[1] for item in chain_batch]
				dbc.executemany(stmt_template, params_list)
				db.commit()
				
				#_logger.info('Chain %s: Inserted %d shares (batch)', chain_id, len(chain_batch))
				return
			
			except Exception as e:
				attempts += 1
				_logger.error('Chain %s: Batch insert failed (attempt %d/%d): %s', 
							  chain_id, attempts, sql.MAX_RECONNECT_ATTEMPTS, e)
				
				if attempts < sql.MAX_RECONNECT_ATTEMPTS:
					_logger.info('Waiting %.1fs before reconnect...', sql.RECONNECT_DELAY)
					sleep(sql.RECONNECT_DELAY)
					try:
						db = sql._reconnect(chain_id)
						db_connections[chain_id] = db
					except Exception as reconnect_error:
						_logger.error('Reconnection failed: %s', reconnect_error)
				else:
					# Max attempts reached - fallback to individual
					_logger.critical('Chain %s: Max reconnection attempts reached, ' +
									 'falling back to individual inserts', chain_id)
					sql._fallback_individual_inserts(chain_id, chain_batch)
					return
				_logger.error('Chain %s: Batch insert failed (attempt %d/%d): %s', 
							  chain_id, attempts, sql.MAX_RECONNECT_ATTEMPTS, e)
				
				if attempts < sql.MAX_RECONNECT_ATTEMPTS:
					_logger.info('Waiting %.1fs before reconnect...', sql.RECONNECT_DELAY)
					sleep(sql.RECONNECT_DELAY)
					try:
						db = sql._reconnect(chain_id)
						db_connections[chain_id] = db
					except Exception as reconnect_error:
						_logger.error('Reconnection failed: %s', reconnect_error)
				else:
					# Max attempts reached - fallback to individual
					_logger.critical('Chain %s: Max reconnection attempts reached, ' +
									 'falling back to individual inserts', chain_id)
					sql._fallback_individual_inserts(chain_id, chain_batch)
					return
	
	@staticmethod
	def _fallback_individual_inserts(chain_id, chain_batch):
		"""
		Fallback to individual inserts when batch fails.
		Maximizes data survival at the cost of performance.
		"""
		success_count = 0
		fail_count = 0
		db = None
		
		try:
			# Get a connection for this chain
			db = sql._reconnect(chain_id)
			
			for i, (stmt, params) in enumerate(chain_batch):
				try:
					dbc = db.cursor()
					dbc.execute(stmt, params)
					db.commit()
					success_count += 1
				except Exception as e:
					_logger.error('Chain %s: Individual insert failed: %s', chain_id, e)
					fail_count += 1
					continue
		
		except Exception as e:
			_logger.error('Chain %s: Could not get database connection for fallback: %s', chain_id, e)
			fail_count = len(chain_batch)
			return
		
		finally:
			# Clean up connection
			if db:
				try:
					db.close()
				except:
					pass
		
		if fail_count > 0:
			_logger.warning('Chain %s: Fallback complete - %d success, %d failed', 
							  chain_id, success_count, fail_count)
	
	@staticmethod
	def _reconnect(chain_id):
		"""
		Create a new database connection using stored dbopts.
		"""
		global _dbopts_storage
		
		with _dbopts_lock:
			if chain_id not in _dbopts_storage:
				raise RuntimeError('No dbopts stored for chain %s' % chain_id)
			
			config = _dbopts_storage[chain_id]
			dbopts = config['dbopts']
			mod = config['mod']
		
		# Create and return connection
		try:
			db = mod.connect(**dbopts)
			return db
		except Exception as e:
			_logger.error('Failed to connect to database for chain %s: %s', chain_id, e)
			raise
	
	def setup_mysql(self):
		import cymysql
		dbopts = self.opts.get('dbopts', {})
		if 'passwd' not in dbopts and 'password' in dbopts:
			dbopts['passwd'] = dbopts['password']
			del dbopts['password']
		self.modsetup(cymysql)
		# Store dbopts for shared writer to use
		self.dbopts = dbopts
	
	def setup_postgres(self):
		import psycopg2
		self.opts.setdefault('statement', 
			"insert into shares (rem_host, username, our_result, upstream_result, " +
			"reason, solution) values ({Q(remoteHost)}, {username}, {YN(not(rejectReason))}, " +
			"{YN(upstreamResult)}, {rejectReason}, decode({solution}, 'hex'))")
		self.modsetup(psycopg2)
		self.dbopts = self.opts.get('dbopts', {})
	
	def setup_sqlite(self):
		import sqlite3
		self.modsetup(sqlite3)
		self.dbopts = self.opts.get('dbopts', {})
	
	def modsetup(self, mod):
		self._mod = mod
		psf = self._psf[mod.paramstyle]
		self.opts.setdefault('statement', 
			"insert into shares (remoteHost, username, rejectReason, upstreamResult, solution) " +
			"values ({remoteHost}, {username}, {rejectReason}, {upstreamResult}, {solution})")
		stmt = self.opts['statement']
		self.pstmt = shareLogFormatter(stmt, psf)
	
	def logShare(self, share):
		o = self.pstmt.applyToShare(share)
		self._logShareF(o)
	
	def stop(self):
		"""
		Gracefully shutdown this instance.
		Only shuts down the shared writer when all instances are stopped.
		"""
		global _writer_thread, _shared_queue, _instance_count
		
		with _instance_lock:
			_instance_count -= 1
			remaining = _instance_count
		
		_logger.info('Chain %s stopping. %d instance(s) remaining.', 
					  self.chain_id, remaining)
		
		if remaining <= 0:
			# Last instance - signal writer to shutdown
			_logger.info('Last instance stopped, signaling shared writer shutdown')
			try:
				_shared_queue.put(None, timeout=5.0)  # Signal shutdown
			except:
				pass
			
			# Wait for writer to finish
			if _writer_thread and _writer_thread.is_alive():
				_writer_thread.join(timeout=10.0)
				if _writer_thread.is_alive():
					_logger.warning('Shared writer thread did not stop gracefully')


