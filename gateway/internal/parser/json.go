package parser

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
)

// Object is an insertion-ordered JSON object.
//
// The Python reference keeps upstream documents in dicts, which preserve the
// key order of the source document; encoding/json maps do not. To stay
// observably identical to the Python gateway (same bytes on the wire, same
// field order after projection) upstream JSON is decoded into Objects.
type Object struct {
	keys []string
	vals map[string]any
}

// NewObject returns an empty ordered object.
func NewObject() *Object {
	return &Object{vals: make(map[string]any)}
}

// Set stores a value, keeping the position of an already known key.
func (o *Object) Set(key string, val any) {
	if o.vals == nil {
		o.vals = make(map[string]any)
	}
	if _, ok := o.vals[key]; !ok {
		o.keys = append(o.keys, key)
	}
	o.vals[key] = val
}

// Get returns the value stored under key.
func (o *Object) Get(key string) (any, bool) {
	if o == nil {
		return nil, false
	}
	v, ok := o.vals[key]
	return v, ok
}

// Delete removes a key and reports whether it was present.
func (o *Object) Delete(key string) bool {
	if o == nil {
		return false
	}
	if _, ok := o.vals[key]; !ok {
		return false
	}
	delete(o.vals, key)
	for i, k := range o.keys {
		if k == key {
			o.keys = append(o.keys[:i], o.keys[i+1:]...)
			break
		}
	}
	return true
}

// Keys returns the keys in insertion order.
func (o *Object) Keys() []string {
	if o == nil {
		return nil
	}
	return o.keys
}

// Len returns the number of keys.
func (o *Object) Len() int {
	if o == nil {
		return 0
	}
	return len(o.keys)
}

// Values returns the values in insertion order.
func (o *Object) Values() []any {
	if o == nil {
		return nil
	}
	out := make([]any, 0, len(o.keys))
	for _, k := range o.keys {
		out = append(out, o.vals[k])
	}
	return out
}

// MarshalJSON renders the object with its keys in insertion order.
func (o *Object) MarshalJSON() ([]byte, error) {
	if o == nil {
		return []byte("null"), nil
	}
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, k := range o.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		buf.Write(encodeString(k))
		buf.WriteByte(':')
		b, err := encodeValue(o.vals[k])
		if err != nil {
			return nil, err
		}
		buf.Write(b)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

// Marshal encodes a value as compact JSON, byte-compatible with
// json.dumps(value, ensure_ascii=False, separators=(",", ":")).
func Marshal(v any) ([]byte, error) {
	return encodeValue(v)
}

func encodeValue(v any) ([]byte, error) {
	switch t := v.(type) {
	case nil:
		return []byte("null"), nil
	case bool:
		if t {
			return []byte("true"), nil
		}
		return []byte("false"), nil
	case json.Number:
		s := t.String()
		if s == "" {
			return nil, fmt.Errorf("invalid json number %q", s)
		}
		return []byte(s), nil
	case string:
		return encodeString(t), nil
	case *Object:
		return t.MarshalJSON()
	case []any:
		var buf bytes.Buffer
		buf.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			b, err := encodeValue(item)
			if err != nil {
				return nil, err
			}
			buf.Write(b)
		}
		buf.WriteByte(']')
		return buf.Bytes(), nil
	case []string:
		items := make([]any, len(t))
		for i, s := range t {
			items[i] = s
		}
		return encodeValue(items)
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var buf bytes.Buffer
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			buf.Write(encodeString(k))
			buf.WriteByte(':')
			b, err := encodeValue(t[k])
			if err != nil {
				return nil, err
			}
			buf.Write(b)
		}
		buf.WriteByte('}')
		return buf.Bytes(), nil
	case map[string]string:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var buf bytes.Buffer
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			buf.Write(encodeString(k))
			buf.WriteByte(':')
			buf.Write(encodeString(t[k]))
		}
		buf.WriteByte('}')
		return buf.Bytes(), nil
	case int:
		return []byte(strconv.Itoa(t)), nil
	case int64:
		return []byte(strconv.FormatInt(t, 10)), nil
	case float64:
		return []byte(formatFloat(t)), nil
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return nil, err
		}
		return b, nil
	}
}

// formatFloat mirrors Python's repr() for floats closely enough for the
// values that appear in upstream documents.
func formatFloat(f float64) string {
	if f == float64(int64(f)) && f < 1e15 && f > -1e15 {
		return strconv.FormatFloat(f, 'f', 1, 64)
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

// encodeString mirrors json.dumps(..., ensure_ascii=False) string escaping.
func encodeString(s string) []byte {
	var buf bytes.Buffer
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
	return buf.Bytes()
}

// UnmarshalOrdered decodes JSON into ordered objects.
//
// Numbers are decoded as json.Number so that the original literal is
// preserved ("1726791" stays "1726791" and does not become "1.726791e+06"),
// matching the Python behaviour of int/float round-tripping.
func UnmarshalOrdered(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := decodeValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("trailing data after JSON document")
		}
		return nil, err
	}
	return v, nil
}

func decodeValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			obj := NewObject()
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := kt.(string)
				if !ok {
					return nil, fmt.Errorf("object key is not a string: %v", kt)
				}
				val, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				obj.Set(key, val)
			}
			if _, err := dec.Token(); err != nil { // closing '}'
				return nil, err
			}
			return obj, nil
		case '[':
			arr := []any{}
			for dec.More() {
				val, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				arr = append(arr, val)
			}
			if _, err := dec.Token(); err != nil { // closing ']'
				return nil, err
			}
			return arr, nil
		default:
			return nil, fmt.Errorf("unexpected delimiter %v", t)
		}
	default:
		return tok, nil
	}
}

// ToInt converts a decoded JSON number to an int, mirroring
// int(node) in the Python reference. Booleans are not numbers.
func ToInt(v any) (int, bool) {
	switch t := v.(type) {
	case json.Number:
		if i, err := t.Int64(); err == nil {
			return int(i), true
		}
		if f, err := t.Float64(); err == nil {
			return int(f), true
		}
	case int:
		return t, true
	case int64:
		return int(t), true
	case float64:
		return int(t), true
	}
	return 0, false
}

// IsNumber reports whether the value is a JSON number (not a boolean).
func IsNumber(v any) bool {
	switch v.(type) {
	case json.Number, int, int64, float64:
		return true
	}
	return false
}

// pyRepr renders a value the way Python's repr() would.
//
// It is used to build cache keys that are byte-identical to the Python
// reference implementation (Resolver._cache_key embeds sorted(params.items())).
func pyRepr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case json.Number:
		return t.String()
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case float64:
		return formatPyFloat(t)
	case string:
		return pyReprString(t)
	case []any:
		parts := make([]string, len(t))
		for i, item := range t {
			parts[i] = pyRepr(item)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case *Object:
		parts := make([]string, 0, t.Len())
		for _, k := range t.Keys() {
			val, _ := t.Get(k)
			parts = append(parts, pyReprString(k)+": "+pyRepr(val))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			parts = append(parts, pyReprString(k)+": "+pyRepr(t[k]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	default:
		return fmt.Sprintf("%v", t)
	}
}

func formatPyFloat(f float64) string {
	if f == float64(int64(f)) && f < 1e15 && f > -1e15 {
		return strconv.FormatFloat(f, 'f', 1, 64)
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

func pyReprString(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var buf bytes.Buffer
	buf.WriteByte(quote)
	for _, r := range s {
		switch r {
		case '\\':
			buf.WriteString(`\\`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r == rune(quote) {
				buf.WriteByte('\\')
				buf.WriteRune(r)
			} else if r < 0x20 {
				fmt.Fprintf(&buf, `\x%02x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte(quote)
	return buf.String()
}
