package parser

import (
	"bytes"
	"strings"
)

// MarshalIndent renders a value as indented JSON.
//
// The layout matches json.dumps(value, ensure_ascii=False, indent=indent) of
// the Python reference, so that a registry dump of both runtimes can be
// diffed byte by byte.
func MarshalIndent(v any, indent string) ([]byte, error) {
	var b bytes.Buffer
	if err := writeIndented(&b, v, indent, 0); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

func writeIndented(b *bytes.Buffer, v any, indent string, depth int) error {
	switch t := v.(type) {
	case *Object:
		if t.Len() == 0 {
			b.WriteString("{}")
			return nil
		}
		b.WriteString("{\n")
		for i, k := range t.Keys() {
			if i > 0 {
				b.WriteString(",\n")
			}
			b.WriteString(strings.Repeat(indent, depth+1))
			b.Write(encodeString(k))
			b.WriteString(": ")
			val, _ := t.Get(k)
			if err := writeIndented(b, val, indent, depth+1); err != nil {
				return err
			}
		}
		b.WriteString("\n")
		b.WriteString(strings.Repeat(indent, depth))
		b.WriteString("}")
		return nil
	case []string:
		items := make([]any, len(t))
		for i, s := range t {
			items[i] = s
		}
		return writeIndented(b, items, indent, depth)
	case []any:
		if len(t) == 0 {
			b.WriteString("[]")
			return nil
		}
		b.WriteString("[\n")
		for i, item := range t {
			if i > 0 {
				b.WriteString(",\n")
			}
			b.WriteString(strings.Repeat(indent, depth+1))
			if err := writeIndented(b, item, indent, depth+1); err != nil {
				return err
			}
		}
		b.WriteString("\n")
		b.WriteString(strings.Repeat(indent, depth))
		b.WriteString("]")
		return nil
	default:
		enc, err := encodeValue(v)
		if err != nil {
			return err
		}
		b.Write(enc)
		return nil
	}
}
