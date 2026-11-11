package dnsmsg

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestParseQuestion(t *testing.T) {
	query := NewQuery(0xBEEF, "WWW.YouTube.com.", TypeA)
	q, end, err := ParseQuestion(query)
	if err != nil {
		t.Fatal(err)
	}
	if q.Name != "www.youtube.com" || q.Type != TypeA || q.Class != ClassIN || end != len(query) {
		t.Fatalf("got %+v end=%d len=%d", q, end, len(query))
	}
	if ID(query) != 0xBEEF {
		t.Fatal("wrong id")
	}
}

func TestParseRejectsResponsesAndJunk(t *testing.T) {
	query := NewQuery(1, "a.com", TypeA)
	resp := append([]byte(nil), query...)
	resp[2] |= 0x80
	if _, _, err := ParseQuestion(resp); err != ErrNotQuery {
		t.Fatalf("want ErrNotQuery, got %v", err)
	}
	if _, _, err := ParseQuestion(query[:5]); err != ErrShort {
		t.Fatalf("want ErrShort, got %v", err)
	}
	if _, _, err := ParseQuestion(query[:len(query)-2]); err != ErrShort {
		t.Fatalf("want ErrShort for cut question, got %v", err)
	}
}

func TestCompressionPointerLoopIsRejected(t *testing.T) {
	msg := make([]byte, 14)
	binary.BigEndian.PutUint16(msg[4:], 1)
	msg[12], msg[13] = 0xC0, 12
	if _, _, err := ReadName(msg, 12); err != ErrFormat {
		t.Fatalf("want ErrFormat, got %v", err)
	}
}

func TestReadNameFollowsPointer(t *testing.T) {
	msg := NewQuery(1, "youtube.com", TypeA)
	msg = append(msg, 3, 'w', 'w', 'w', 0xC0, 12)
	name, end, err := ReadName(msg, len(msg)-6)
	if err != nil || name != "www.youtube.com" || end != len(msg) {
		t.Fatalf("name=%q end=%d err=%v", name, end, err)
	}
}

func TestBlockedResponseA(t *testing.T) {
	query := NewQuery(7, "youtube.com", TypeA)
	q, end, _ := ParseQuestion(query)
	resp := BlockedResponse(query, q, end)
	if ID(resp) != 7 || resp[2]&0x80 == 0 || Rcode(resp) != RcodeSuccess {
		t.Fatal("bad header")
	}
	if binary.BigEndian.Uint16(resp[6:]) != 1 {
		t.Fatal("want one answer")
	}
	if !bytes.Equal(resp[len(resp)-4:], []byte{0, 0, 0, 0}) {
		t.Fatal("want 0.0.0.0")
	}
	if ttl, ok := MinTTL(resp); !ok || ttl != BlockedTTL {
		t.Fatalf("ttl=%d ok=%v", ttl, ok)
	}
}

func TestBlockedResponseOtherTypeIsNoData(t *testing.T) {
	query := NewQuery(7, "youtube.com", 65)
	q, end, _ := ParseQuestion(query)
	resp := BlockedResponse(query, q, end)
	if binary.BigEndian.Uint16(resp[6:]) != 0 || Rcode(resp) != RcodeSuccess {
		t.Fatal("want empty NOERROR answer")
	}
}

func TestErrorResponse(t *testing.T) {
	query := NewQuery(9, "a.com", TypeA)
	_, end, _ := ParseQuestion(query)
	resp := ErrorResponse(query, end, RcodeServFail)
	if Rcode(resp) != RcodeServFail || ID(resp) != 9 || len(resp) != end {
		t.Fatal("bad SERVFAIL")
	}
	if ErrorResponse([]byte{1, 2}, 0, RcodeFormErr) != nil {
		t.Fatal("tiny packets get no reply")
	}
}

func TestTypeName(t *testing.T) {
	if TypeName(TypeAAAA) != "AAAA" || TypeName(257) != "TYPE257" {
		t.Fatal("type names")
	}
}
