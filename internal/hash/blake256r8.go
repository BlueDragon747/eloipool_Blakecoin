package hash

import (
	"encoding/binary"
	"math/bits"
)

var blake256IV = [8]uint32{
	0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
	0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
}

var blake256C = [16]uint32{
	0x243F6A88, 0x85A308D3, 0x13198A2E, 0x03707344,
	0xA4093822, 0x299F31D0, 0x082EFA98, 0xEC4E6C89,
	0x452821E6, 0x38D01377, 0xBE5466CF, 0x34E90C6C,
	0xC0AC29B7, 0xC97C50DD, 0x3F84D5B5, 0xB5470917,
}

var blakeSigma = [10][16]uint8{
	{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
	{14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3},
	{11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4},
	{7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8},
	{9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13},
	{2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9},
	{12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11},
	{13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10},
	{6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5},
	{10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0},
}

type blake256r8 struct {
	h     [8]uint32
	t     int64
	cache []byte
	nullt bool
}

// Blake256R8 returns the Blakecoin proof-of-work hash: BLAKE-256 with 8
// rounds. It is intentionally not BLAKE2 and not the standard 14-round BLAKE.
func Blake256R8(data []byte) [32]byte {
	d := blake256r8{h: blake256IV}
	d.update(data)
	return d.final()
}

func (d *blake256r8) update(data []byte) {
	if len(data) == 0 {
		return
	}
	left := len(d.cache)
	fill := 64 - left
	if left > 0 && len(data) >= fill {
		d.cache = append(d.cache, data[:fill]...)
		d.t += 512
		d.compress(d.cache)
		d.cache = d.cache[:0]
		data = data[fill:]
	}
	for len(data) >= 64 {
		d.t += 512
		d.compress(data[:64])
		data = data[64:]
	}
	if len(data) > 0 {
		d.cache = append(d.cache, data...)
	}
}

func (d *blake256r8) final() [32]byte {
	tt := d.t + int64(len(d.cache)<<3)
	var msgLen [8]byte
	binary.BigEndian.PutUint64(msgLen[:], uint64(tt))

	padding := make([]byte, 129)
	padding[0] = 0x80
	sizeWithout := 55
	if len(d.cache) == sizeWithout {
		d.t -= 8
		d.update([]byte{0x81})
	} else {
		if len(d.cache) < sizeWithout {
			if len(d.cache) == 0 {
				d.nullt = true
			}
			d.t -= int64((sizeWithout - len(d.cache)) << 3)
			d.update(padding[:sizeWithout-len(d.cache)])
		} else {
			d.t -= int64((64 - len(d.cache)) << 3)
			d.update(padding[:64-len(d.cache)])
			d.t -= int64((sizeWithout + 1) << 3)
			d.update(padding[1 : sizeWithout+1])
			d.nullt = true
		}
		d.update([]byte{0x01})
		d.t -= 8
	}
	d.t -= 64
	d.update(msgLen[:])

	var out [32]byte
	for i, h := range d.h {
		binary.BigEndian.PutUint32(out[i*4:], h)
	}
	return out
}

func (d *blake256r8) compress(block []byte) {
	var m [16]uint32
	for i := range m {
		m[i] = binary.BigEndian.Uint32(block[i*4:])
	}
	var v [16]uint32
	copy(v[:8], d.h[:])
	copy(v[8:], blake256C[:8])
	if !d.nullt {
		lo := uint32(d.t)
		hi := uint32(uint64(d.t) >> 32)
		v[12] ^= lo
		v[13] ^= lo
		v[14] ^= hi
		v[15] ^= hi
	}
	for round := 0; round < 8; round++ {
		g(&v, &m, round, 0, 4, 8, 12, 0)
		g(&v, &m, round, 1, 5, 9, 13, 2)
		g(&v, &m, round, 2, 6, 10, 14, 4)
		g(&v, &m, round, 3, 7, 11, 15, 6)
		g(&v, &m, round, 0, 5, 10, 15, 8)
		g(&v, &m, round, 1, 6, 11, 12, 10)
		g(&v, &m, round, 2, 7, 8, 13, 12)
		g(&v, &m, round, 3, 4, 9, 14, 14)
	}
	for i := range d.h {
		d.h[i] ^= v[i] ^ v[i+8]
	}
}

func g(v *[16]uint32, m *[16]uint32, round, a, b, c, d, i int) {
	s0 := blakeSigma[round][i]
	s1 := blakeSigma[round][i+1]
	v[a] += v[b] + (m[s0] ^ blake256C[s1])
	v[d] = bits.RotateLeft32(v[d]^v[a], -16)
	v[c] += v[d]
	v[b] = bits.RotateLeft32(v[b]^v[c], -12)
	v[a] += v[b] + (m[s1] ^ blake256C[s0])
	v[d] = bits.RotateLeft32(v[d]^v[a], -8)
	v[c] += v[d]
	v[b] = bits.RotateLeft32(v[b]^v[c], -7)
}
