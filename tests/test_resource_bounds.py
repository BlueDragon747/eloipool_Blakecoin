import logging
import tempfile
import unittest
from pathlib import Path

from networkserver import SocketHandler
from sharelogging.logfile import logfile


class TestLogfileBounds(unittest.TestCase):
    def test_logfile_queue_drops_oldest_when_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = logfile(
                str(Path(tmpdir) / "shares.log"),
                max_queue_size=2,
                autostart=False,
            )

            log.queueshare("one\n")
            log.queueshare("two\n")
            log.queueshare("three\n")

            self.assertEqual(list(log.queue), ["two\n", "three\n"])
            self.assertEqual(log.dropped, 1)


class DummyServer:
    MaxWriteBuffer = 4

    def __init__(self):
        self.connections = {}
        self.lastReadbuf = b""
        self.registered = {}

    def register_socket(self, fd, handler, eventmask=None):
        self.registered[fd] = eventmask

    def register_socket_m(self, fd, eventmask):
        self.registered[fd] = eventmask

    def unregister_socket(self, fd):
        self.registered.pop(fd, None)

    def schedule(self, task, startTime, errHandler=None):
        return task

    def rmSchedule(self, task):
        return None


class DummySocket:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return 123

    def send(self, data):
        return 0

    def close(self):
        self.closed = True


class TestSocketWriteBufferBounds(unittest.TestCase):
    def test_push_closes_slow_client_when_write_buffer_exceeds_cap(self):
        server = DummyServer()
        sock = DummySocket()
        handler = SocketHandler(server, sock, ("127.0.0.1", 3334))
        handler.logger = logging.getLogger("test.socket")

        handler.push(b"abcd")
        self.assertEqual(handler.wbuf, b"abcd")
        self.assertFalse(sock.closed)

        handler.push(b"e")

        self.assertTrue(sock.closed)
        self.assertEqual(handler.fd, -1)
        self.assertNotIn(id(handler), server.connections)


if __name__ == "__main__":
    unittest.main()
