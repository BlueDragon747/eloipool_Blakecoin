# Eloipool - Python Bitcoin pool server
# Copyright (C) 2011-2012  Luke Dashjr <luke-jr+eloipool@utopios.org>
# Copyright (C) 2012  Peter Leurs <kinlo@triplemining.com>
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



from collections import deque
from datetime import date
from time import sleep, time
import threading
from util import shareLogFormatter
import logging
import traceback

_logger = logging.getLogger('sharelogging.logfile')

class logfile(threading.Thread):
	DEFAULT_QUEUE_MAXSIZE = 10000
	DROP_WARN_INTERVAL = 60

	def __init__(self, filename, **ka):
		super().__init__(**ka.get('thropts', {}))
		self.fn=filename
		if 'format' not in ka:
			_logger.warning('"format" not specified for logfile logger, but default may vary!')
			ka['format'] = "{time} {Q(remoteHost)} {username} {YN(not(rejectReason))} {dash(YN(upstreamResult))} {dash(rejectReason)} {solution} {target2pdiff(target)}\n"
		self.fmt = shareLogFormatter(ka['format'], '%s')
		self.max_queue_size = int(ka.get('max_queue_size', ka.get('queue_maxsize', self.DEFAULT_QUEUE_MAXSIZE)))
		if self.max_queue_size < 1:
			self.max_queue_size = 1
		self.queue = deque()
		self._queue_lock = threading.Lock()
		self.dropped = 0
		self._last_drop_warning = 0
		if ka.get('autostart', True):
			self.start()
	
	def queueshare(self, line):
		with self._queue_lock:
			if len(self.queue) >= self.max_queue_size:
				self.queue.popleft()
				self.dropped += 1
				now = time()
				if now >= self._last_drop_warning + self.DROP_WARN_INTERVAL:
					_logger.warning(
						'logfile queue full for %s; dropped %d oldest share log line(s)',
						self.fn,
						self.dropped,
					)
					self._last_drop_warning = now
			self.queue.append(line)
	
	def flushlog(self):
		with self._queue_lock:
			if not self.queue:
				return
			lines = list(self.queue)
			self.queue.clear()
		with open(self.fn, "a") as logfile:
			logfile.writelines(lines)
	
	def run(self):
		while True:
			try:
				sleep(0.2)
				self.flushlog()
			except:
				_logger.critical(traceback.format_exc())
	
	def logShare(self, share):
		logline = self.fmt.formatShare(share)
		self.queueshare(logline)
