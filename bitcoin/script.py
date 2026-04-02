# Eloipool - Python Bitcoin pool server
# Copyright (C) 2011-2012  Luke Dashjr <luke-jr+eloipool@utopios.org>
# Portions written by Carlos Pizarro <kr105@kr105.com>
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

from base58 import b58decode
from util import blakehash
WitnessMagic = b'\xaa\x21\xa9\xed'

def _Address2PKH(addr):
	try:
		addr = b58decode(addr, 25)
	except:
		return None
	if addr is None:
		return None
	ver = addr[0]
	cksumA = addr[-4:]
	cksumB = blakehash(addr[:-4])[:4]
	if cksumA != cksumB:
		return None
	return (ver, addr[1:-4])

class BitcoinScript:
	# Blakecoin address versions
	# Mainnet P2PKH = 25/26 (0x19/0x1a) - note: 26 seen in some addresses
	# Testnet P2PKH = 65 (0x41)
	# Mainnet P2SH = 22 (0x16)
	# Testnet P2SH = 127 (0x7f)
	P2PKH_VERSIONS = (0, 25, 26, 65, 111)  # Include Bitcoin versions for compatibility
	P2SH_VERSIONS = (5, 22, 127, 196)
	
	@classmethod
	def toAddress(cls, addr):
		d = _Address2PKH(addr)
		if not d:
			raise ValueError('invalid address')
		(ver, pubkeyhash) = d
		# Accept both Bitcoin and Blakecoin address versions
		if ver in cls.P2PKH_VERSIONS:
			return b'\x76\xa9\x14' + pubkeyhash + b'\x88\xac'
		elif ver in cls.P2SH_VERSIONS:
			return b'\xa9\x14' + pubkeyhash + b'\x87'
		raise ValueError('invalid address version: %d (expected P2PKH: %s or P2SH: %s)' % (ver, cls.P2PKH_VERSIONS, cls.P2SH_VERSIONS))
	
	@classmethod
	def commitment(cls, commitment):
		clen = len(commitment)
		if clen > 0x4b:
			raise NotImplementedError
		return b'\x6a' + bytes((clen,)) + commitment

def countSigOps(s):
	# FIXME: don't count data as ops
	c = 0
	for ch in s:
		if 0xac == ch & 0xfe:
			c += 1
		elif 0xae == ch & 0xfe:
			c += 20
	return c

# NOTE: This does not work for signed numbers (set the high bit) or zero (use b'\0')
def encodeUNum(n):
	s = bytearray(b'\1')
	while n > 127:
		s[0] += 1
		s.append(n % 256)
		n //= 256
	s.append(n)
	return bytes(s)

def encodeNum(n):
	if n == 0:
		return b'\0'
	if n > 0:
		return encodeUNum(n)
	s = encodeUNum(abs(n))
	s = bytearray(s)
	s[-1] = s[-1] | 0x80
	return bytes(s)

# tests
def _test():
	assert b'\0' == encodeNum(0)
	assert b'\1\x55' == encodeNum(0x55)
	assert b'\2\xfd\0' == encodeNum(0xfd)
	assert b'\3\xff\xff\0' == encodeNum(0xffff)
	assert b'\3\0\0\x01' == encodeNum(0x10000)
	assert b'\5\xff\xff\xff\xff\0' == encodeNum(0xffffffff)

_test()
