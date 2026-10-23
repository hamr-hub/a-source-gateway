package registry

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// nfkdASCII holds the NFKD decompositions of the Latin-1 Supplement and
// Latin Extended-A blocks that reduce to pure ASCII.
//
// The Python reference normalises names with unicodedata.normalize("NFKD", ...)
// before slugifying. Go's standard library has no Unicode normalisation, so
// the relevant part of the decomposition table is embedded here. Code points
// outside the table are treated as separators, which is exactly what the
// Python reference does for every character that NFKD leaves non-ASCII
// (Chinese characters, for instance).
var nfkdASCII = map[rune]string{}

func init() {
	for _, item := range strings.Split(nfkdTable, ",") {
		parts := strings.SplitN(item, ":", 2)
		if len(parts) != 2 {
			continue
		}
		cp, err := strconv.ParseInt(parts[0], 16, 32)
		if err != nil {
			continue
		}
		nfkdASCII[rune(cp)] = parts[1]
	}
}

const nfkdTable = "00C0:A,00C1:A,00C2:A,00C3:A,00C4:A,00C5:A,00C7:C,00C8:E,00C9:E,00CA:E,00CB:E,00CC:I,00CD:I,00CE:I,00CF:I,00D1:N,00D2:O,00D3:O,00D4:O,00D5:O,00D6:O,00D9:U,00DA:U,00DB:U,00DC:U,00DD:Y,00E0:a,00E1:a,00E2:a,00E3:a,00E4:a,00E5:a,00E7:c,00E8:e,00E9:e,00EA:e,00EB:e,00EC:i,00ED:i,00EE:i,00EF:i,00F1:n,00F2:o,00F3:o,00F4:o,00F5:o,00F6:o,00F9:u,00FA:u,00FB:u,00FC:u,00FD:y,00FF:y,0100:A,0101:a,0102:A,0103:a,0104:A,0105:a,0106:C,0107:c,0108:C,0109:c,010A:C,010B:c,010C:C,010D:c,010E:D,010F:d,0112:E,0113:e,0114:E,0115:e,0116:E,0117:e,0118:E,0119:e,011A:E,011B:e,011C:G,011D:g,011E:G,011F:g,0120:G,0121:g,0122:G,0123:g,0124:H,0125:h,0128:I,0129:i,012A:I,012B:i,012C:I,012D:i,012E:I,012F:i,0130:I,0132:IJ,0133:ij,0134:J,0135:j,0136:K,0137:k,0139:L,013A:l,013B:L,013C:l,013D:L,013E:l,0143:N,0144:n,0145:N,0146:n,0147:N,0148:n,014C:O,014D:o,014E:O,014F:o,0150:O,0151:o,0154:R,0155:r,0156:R,0157:r,0158:R,0159:r,015A:S,015B:s,015C:S,015D:s,015E:S,015F:s,0160:S,0161:s,0162:T,0163:t,0164:T,0165:t,0168:U,0169:u,016A:U,016B:u,016C:U,016D:u,016E:U,016F:u,0170:U,0171:u,0172:U,0173:u,0174:W,0175:w,0176:Y,0177:y,0178:Y,0179:Z,017A:z,017B:Z,017C:z,017D:Z,017E:z,017F:s,01A0:O,01A1:o,01AF:U,01B0:u,01C4:DZ,01C5:Dz,01C6:dz,01C7:LJ,01C8:Lj,01C9:lj,01CA:NJ,01CB:Nj,01CC:nj,01CD:A,01CE:a,01CF:I,01D0:i,01D1:O,01D2:o,01D3:U,01D4:u,01D5:U,01D6:u,01D7:U,01D8:u,01D9:U,01DA:u,01DB:U,01DC:u,01DE:A,01DF:a,01E0:A,01E1:a,01E6:G,01E7:g,01E8:K,01E9:k,01EA:O,01EB:o,01EC:O,01ED:o,01F0:j,01F1:DZ,01F2:Dz,01F3:dz,01F4:G,01F5:g,01F8:N,01F9:n,01FA:A,01FB:a,0200:A,0201:a,0202:A,0203:a,0204:E,0205:e,0206:E,0207:e,0208:I,0209:i,020A:I,020B:i,020C:O,020D:o,020E:O,020F:o,0210:R,0211:r,0212:R,0213:r,0214:U,0215:u,0216:U,0217:u,0218:S,0219:s,021A:T,021B:t,021E:H,021F:h,0226:A,0227:a,0228:E,0229:e,022A:O,022B:o,022C:O,022D:o,022E:O,022F:o,0230:O,0231:o,0232:Y,0233:y"

// aliasPattern matches the aliases that may be used as a source id.
var aliasPattern = regexp.MustCompile(`^[a-z0-9_]{3,}$`)

// unitMap maps the Chinese scale keywords to the English units.
// The order matters: the first match wins.
var unitMap = []struct {
	kw   string
	unit string
}{
	{"取值", "values"},
	{"张表", "tables"},
	{"表", "tables"},
	{"份", "filings"},
	{"项", "items"},
	{"篇", "papers"},
	{"条", "records"},
	{"家", "entities"},
	{"个", "records"},
	{"种", "types"},
	{"笔", "transactions"},
	{"件", "items"},
	{"国", "countries"},
	{"位", "people"},
}

// countUnits are the characters that may follow a number in the measured
// scale column.
const countUnits = "份条项个家种笔件国位机构标目页版款座套"

// authRule is one entry of the ordered authentication rule table.
type authRule struct {
	needle string
	typ    string
	opt    bool
}

// authRules is the ordered rule table of the Python reference.
//
// The order is significant: the first matching rule wins, which is why
// "无key" has to be checked before the generic "key" rules.
var authRules = []authRule{
	{"无key", "none", false},
	{"无 key", "none", false},
	{"无需", "none", false},
	{"需邮箱", "email_param", false},
	{"邮箱", "email_param", false},
	{"可选key", "api_key", true},
	{"可注册key", "api_key", false},
	{"免费key", "api_key", false},
	{"api key", "api_key", false},
	{"需token", "token", false},
	{"token", "oauth2", false},
	{"oauth", "oauth2", false},
	{"部分付费", "paid", false},
	{"需付费", "paid", false},
	{"账号+付费", "paid", false},
	{"免费/付费", "mixed", false},
	{"部分免费部分付费", "mixed", false},
	{"多数免费", "mixed", false},
	{"部分注册", "mixed", false},
}

// Slugify derives a stable source id from a source name.
//
// ASCII-fold the name, replace every run of non [A-Za-z0-9] characters with
// "_", trim "_" and lowercase. When the result is shorter than three
// characters the first alias matching ^[a-z0-9_]{3,}$ is used, otherwise the
// zero padded row index.
func Slugify(name string, idx int, aliases []string) string {
	s := foldSlug(name)
	if len(s) >= 3 {
		return s
	}
	for _, a := range aliases {
		a = strings.ToLower(strings.TrimSpace(a))
		if aliasPattern.MatchString(a) {
			return a
		}
	}
	return fmt.Sprintf("src_%03d", idx)
}

// foldSlug implements the ASCII folding and slug rules of the reference.
func foldSlug(name string) string {
	var b strings.Builder
	sep := false
	for _, r := range name {
		switch {
		case r < 128:
			if isASCIIAlnum(r) {
				b.WriteRune(r)
				sep = false
				continue
			}
		default:
			if repl, ok := nfkdASCII[r]; ok {
				b.WriteString(repl)
				sep = false
				continue
			}
		}
		if !sep && b.Len() > 0 {
			b.WriteByte('_')
			sep = true
		}
	}
	return strings.ToLower(strings.Trim(b.String(), "_"))
}

func isASCIIAlnum(r rune) bool {
	return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9')
}

// ClassifyAuth maps the raw authentication column onto (type, optional).
//
// The rule order is significant. A naive `"key" in text` check would classify
// the 65 "无key" rows as key-protected; that is the documented historical
// bug of the Python implementation and is avoided here.
func ClassifyAuth(text string) (string, bool) {
	t := strings.ToLower(strings.TrimSpace(text))
	if t == "" || t == "无" || t == "none" || t == "-" {
		return "none", false
	}
	// "无key(可选)" and friends: the key is optional, so this has to be
	// checked before the "无key" rule.
	if strings.Contains(t, "可选") && strings.Contains(t, "key") {
		return "api_key", true
	}
	for _, rule := range authRules {
		if strings.Contains(t, rule.needle) {
			return rule.typ, rule.opt
		}
	}
	return "none", false
}

// ParseTotal extracts the main record count from the measured scale column.
//
// It strips thousands separators, looks at the text behind "实测", collects
// every "<number><unit><count-unit>" match, scales it by 万/亿/千, keeps the
// values of at least 100 and returns the largest one. When nothing matches it
// falls back to the longest run of four or more digits.
func ParseTotal(text string) (int, bool) {
	if text == "" {
		return 0, false
	}
	text = stripThousandsSeparators(text)
	seg := text
	if idx := strings.Index(text, "实测"); idx >= 0 {
		seg = text[idx+len("实测"):]
	}

	cands := []int{}
	i := 0
	for i < len(seg) {
		if !isDigit(seg[i]) {
			i++
			continue
		}
		start := i
		for i < len(seg) && isDigit(seg[i]) {
			i++
		}
		numText := seg[start:i]
		if i < len(seg) && seg[i] == '.' {
			j := i + 1
			for j < len(seg) && isDigit(seg[j]) {
				j++
			}
			if j > i+1 {
				numText += seg[i:j]
				i = j
			}
		}
		j := skipSpace(seg, i)
		mult := 1
		switch {
		case strings.HasPrefix(seg[j:], "万"):
			mult = 10000
			j += len("万")
		case strings.HasPrefix(seg[j:], "亿"):
			mult = 100000000
			j += len("亿")
		case strings.HasPrefix(seg[j:], "千"):
			mult = 1000
			j += len("千")
		}
		j = skipSpace(seg, j)
		if j < len(seg) {
			r, _ := utf8.DecodeRuneInString(seg[j:])
			if isCountUnit(r) {
				if f, err := strconv.ParseFloat(numText, 64); err == nil {
					n := int(f * float64(mult))
					if n >= 100 {
						cands = append(cands, n)
					}
				}
			}
		}
	}
	if len(cands) == 0 {
		for _, run := range digitRuns(seg, 4) {
			cands = append(cands, run)
		}
	}
	if len(cands) == 0 {
		return 0, false
	}
	max := cands[0]
	for _, c := range cands[1:] {
		if c > max {
			max = c
		}
	}
	return max, true
}

// ParseUnit maps the measured scale column onto an English unit.
// The first matching keyword of the unit table wins.
func ParseUnit(text string) string {
	for _, m := range unitMap {
		if strings.Contains(text, m.kw) {
			return m.unit
		}
	}
	return ""
}

// stripThousandsSeparators removes the thousand separators of a number.
//
// Only a comma between two digits that is followed by exactly three digits
// is removed, which mirrors the reference regular expression
// (?<=\d),(?=\d{3}(?!\d)).
func stripThousandsSeparators(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); {
		if s[i] == ',' && i > 0 && isDigit(s[i-1]) && i+3 < len(s) &&
			isDigit(s[i+1]) && isDigit(s[i+2]) && isDigit(s[i+3]) {
			after := i + 4
			if after >= len(s) || !isDigit(s[after]) {
				i++
				continue
			}
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

// digitRuns returns every run of at least min digits as an integer.
func digitRuns(s string, min int) []int {
	var out []int
	i := 0
	for i < len(s) {
		if !isDigit(s[i]) {
			i++
			continue
		}
		start := i
		for i < len(s) && isDigit(s[i]) {
			i++
		}
		if i-start >= min {
			if n, err := strconv.Atoi(s[start:i]); err == nil {
				out = append(out, n)
			}
		}
	}
	return out
}

func isDigit(b byte) bool { return b >= '0' && b <= '9' }

func isCountUnit(r rune) bool { return strings.ContainsRune(countUnits, r) }

// skipSpace skips the whitespace characters of the reference regular
// expression (\s).
func skipSpace(s string, i int) int {
	for i < len(s) {
		switch s[i] {
		case ' ', '\t', '\n', '\r', '\f', '\v':
			i++
		default:
			return i
		}
	}
	return i
}
