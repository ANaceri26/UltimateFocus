package blocklist

import (
	"bufio"
	"io"
	"net"
	"strings"
)

var sinkAddresses = map[string]bool{"0.0.0.0": true, "127.0.0.1": true, "::": true, "::1": true}

// ParseDomains reads one domain per line, or hosts-file lines like "0.0.0.0 youtube.com".
// Comments start with '#'. Hosts lines that map a name to a real IP are skipped.
func ParseDomains(r io.Reader) ([]string, error) {
	var out []string
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := scanner.Text()
		if i := strings.IndexByte(line, '#'); i >= 0 {
			line = line[:i]
		}
		fields := strings.Fields(line)
		switch {
		case len(fields) == 0:
		case sinkAddresses[fields[0]]:
			out = append(out, fields[1:]...)
		case net.ParseIP(fields[0]) != nil:
		default:
			out = append(out, fields...)
		}
	}
	return out, scanner.Err()
}
