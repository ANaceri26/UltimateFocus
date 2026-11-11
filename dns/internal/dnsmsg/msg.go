// Package dnsmsg reads and writes the DNS wire format (RFC 1035) by hand.
//
// A DNS message is a 12-byte header followed by sections:
//
//	0       2       4        6        8        10       12
//	| ID    | flags | QDCOUNT| ANCOUNT| NSCOUNT| ARCOUNT| question ... answers ...
//
// Names are sequences of length-prefixed labels ending in a zero byte:
// "www.youtube.com" = 3 'w' 'w' 'w' 7 'y' 'o' 'u' 't' 'u' 'b' 'e' 3 'c' 'o' 'm' 0.
// To save space a name may instead end with a 2-byte pointer (top two bits 11) to an
// earlier offset in the message ("compression").
package dnsmsg

import (
	"encoding/binary"
	"errors"
	"strconv"
	"strings"
)

const (
	TypeA    uint16 = 1
	TypeAAAA uint16 = 28
	ClassIN  uint16 = 1

	RcodeSuccess  = 0
	RcodeFormErr  = 1
	RcodeServFail = 2
	RcodeNXDomain = 3

	BlockedTTL = 60

	headerLen      = 12
	maxNameLen     = 255
	maxPointerHops = 32
)

var (
	ErrShort    = errors.New("dns: message too short")
	ErrFormat   = errors.New("dns: malformed message")
	ErrNotQuery = errors.New("dns: not a standard query")
)

type Question struct {
	Name  string
	Type  uint16
	Class uint16
}

func ID(msg []byte) uint16 {
	if len(msg) < 2 {
		return 0
	}
	return binary.BigEndian.Uint16(msg)
}

func SetID(msg []byte, id uint16) {
	if len(msg) >= 2 {
		binary.BigEndian.PutUint16(msg, id)
	}
}

func Rcode(msg []byte) int {
	if len(msg) < 4 {
		return -1
	}
	return int(msg[3] & 0x0F)
}

func TypeName(t uint16) string {
	switch t {
	case TypeA:
		return "A"
	case TypeAAAA:
		return "AAAA"
	case 5:
		return "CNAME"
	case 15:
		return "MX"
	case 16:
		return "TXT"
	case 65:
		return "HTTPS"
	default:
		return "TYPE" + strconv.Itoa(int(t))
	}
}

// ParseQuestion validates a standard query with exactly one question and returns it,
// plus the offset just past the question section.
func ParseQuestion(msg []byte) (Question, int, error) {
	if len(msg) < headerLen {
		return Question{}, 0, ErrShort
	}
	flags := binary.BigEndian.Uint16(msg[2:4])
	if flags&0x8000 != 0 || (flags>>11)&0x0F != 0 {
		return Question{}, 0, ErrNotQuery
	}
	if binary.BigEndian.Uint16(msg[4:6]) != 1 {
		return Question{}, 0, ErrFormat
	}
	name, off, err := ReadName(msg, headerLen)
	if err != nil {
		return Question{}, 0, err
	}
	if off+4 > len(msg) {
		return Question{}, 0, ErrShort
	}
	q := Question{
		Name:  name,
		Type:  binary.BigEndian.Uint16(msg[off:]),
		Class: binary.BigEndian.Uint16(msg[off+2:]),
	}
	return q, off + 4, nil
}

// ReadName decodes a possibly-compressed name starting at off. It returns the name and
// the offset just after the name *as it appears at off* (a pointer counts as 2 bytes).
// Pointer hops are capped so a malicious loop cannot hang the server.
func ReadName(msg []byte, off int) (string, int, error) {
	var parts []string
	end, hops, length := -1, 0, 0
	for {
		if off >= len(msg) {
			return "", 0, ErrShort
		}
		b := int(msg[off])
		switch {
		case b == 0:
			if end < 0 {
				end = off + 1
			}
			return strings.ToLower(strings.Join(parts, ".")), end, nil
		case b&0xC0 == 0xC0:
			if off+1 >= len(msg) {
				return "", 0, ErrShort
			}
			if end < 0 {
				end = off + 2
			}
			hops++
			if hops > maxPointerHops {
				return "", 0, ErrFormat
			}
			off = int(binary.BigEndian.Uint16(msg[off:]) & 0x3FFF)
		case b&0xC0 != 0:
			return "", 0, ErrFormat
		default:
			off++
			if off+b > len(msg) {
				return "", 0, ErrShort
			}
			length += b + 1
			if length > maxNameLen {
				return "", 0, ErrFormat
			}
			parts = append(parts, string(msg[off:off+b]))
			off += b
		}
	}
}

func setResponseFlags(resp, query []byte, rcode int) {
	orig := binary.BigEndian.Uint16(query[2:4])
	flags := uint16(0x8000) | orig&0x7800 | orig&0x0100 | 0x0080 | uint16(rcode&0x0F)
	binary.BigEndian.PutUint16(resp[2:4], flags)
}

// BlockedResponse answers A with 0.0.0.0 and AAAA with ::, the same "sink" address the
// hosts file uses. Other types (HTTPS, MX...) get an empty answer (NODATA).
func BlockedResponse(query []byte, q Question, qEnd int) []byte {
	resp := make([]byte, qEnd, qEnd+28)
	copy(resp, query[:qEnd])
	setResponseFlags(resp, query, RcodeSuccess)
	binary.BigEndian.PutUint16(resp[4:], 1)
	binary.BigEndian.PutUint16(resp[6:], 0)
	binary.BigEndian.PutUint16(resp[8:], 0)
	binary.BigEndian.PutUint16(resp[10:], 0)
	var rdata []byte
	switch q.Type {
	case TypeA:
		rdata = make([]byte, 4)
	case TypeAAAA:
		rdata = make([]byte, 16)
	}
	if rdata == nil || q.Class != ClassIN {
		return resp
	}
	binary.BigEndian.PutUint16(resp[6:], 1)
	resp = append(resp, 0xC0, headerLen)
	resp = binary.BigEndian.AppendUint16(resp, q.Type)
	resp = binary.BigEndian.AppendUint16(resp, ClassIN)
	resp = binary.BigEndian.AppendUint32(resp, BlockedTTL)
	resp = binary.BigEndian.AppendUint16(resp, uint16(len(rdata)))
	return append(resp, rdata...)
}

// ErrorResponse builds a reply carrying only an error code (FORMERR, SERVFAIL...).
func ErrorResponse(query []byte, qEnd int, rcode int) []byte {
	if len(query) < headerLen {
		return nil
	}
	if qEnd < headerLen || qEnd > len(query) {
		qEnd = headerLen
	}
	resp := make([]byte, qEnd)
	copy(resp, query[:qEnd])
	setResponseFlags(resp, query, rcode)
	var qd uint16
	if qEnd > headerLen {
		qd = 1
	}
	binary.BigEndian.PutUint16(resp[4:], qd)
	for i := 6; i < headerLen; i++ {
		resp[i] = 0
	}
	return resp
}

// MinTTL returns the smallest TTL in the answer and authority sections, which is how
// long the whole response may be cached.
func MinTTL(msg []byte) (uint32, bool) {
	if len(msg) < headerLen {
		return 0, false
	}
	qd := int(binary.BigEndian.Uint16(msg[4:]))
	records := int(binary.BigEndian.Uint16(msg[6:])) + int(binary.BigEndian.Uint16(msg[8:]))
	off := headerLen
	for i := 0; i < qd; i++ {
		_, next, err := ReadName(msg, off)
		if err != nil {
			return 0, false
		}
		off = next + 4
	}
	var lowest uint32
	found := false
	for i := 0; i < records; i++ {
		_, next, err := ReadName(msg, off)
		if err != nil || next+10 > len(msg) {
			return 0, false
		}
		ttl := binary.BigEndian.Uint32(msg[next+4:])
		off = next + 10 + int(binary.BigEndian.Uint16(msg[next+8:]))
		if off > len(msg) {
			return 0, false
		}
		if !found || ttl < lowest {
			lowest, found = ttl, true
		}
	}
	return lowest, found
}

// NewQuery builds a recursive query for name (used by tests and simple clients).
func NewQuery(id uint16, name string, qtype uint16) []byte {
	msg := make([]byte, headerLen, headerLen+len(name)+6)
	binary.BigEndian.PutUint16(msg[0:], id)
	binary.BigEndian.PutUint16(msg[2:], 0x0100)
	binary.BigEndian.PutUint16(msg[4:], 1)
	for _, label := range strings.Split(strings.TrimSuffix(name, "."), ".") {
		if label == "" || len(label) > 63 {
			continue
		}
		msg = append(msg, byte(len(label)))
		msg = append(msg, label...)
	}
	msg = append(msg, 0)
	msg = binary.BigEndian.AppendUint16(msg, qtype)
	return binary.BigEndian.AppendUint16(msg, ClassIN)
}
