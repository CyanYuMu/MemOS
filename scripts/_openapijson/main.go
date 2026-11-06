package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/getkin/kin-openapi/openapi3"
)

func main() {
	in := flag.String("in", "", "源 YAML")
	out := flag.String("out", "", "目标 JSON")
	flag.Parse()
	if *in == "" || *out == "" {
		fmt.Fprintln(os.Stderr, "用法: go run ./scripts/_openapijson -in api/openapi.dashboard.yaml -out <path>")
		os.Exit(2)
	}
	doc, err := openapi3.NewLoader().LoadFromFile(*in)
	if err != nil {
		fmt.Fprintf(os.Stderr, "加载 %s: %v\n", *in, err)
		os.Exit(1)
	}
	if err := doc.Validate(context.Background()); err != nil {
		fmt.Fprintf(os.Stderr, "校验 %s: %v\n", *in, err)
		os.Exit(1)
	}
	raw, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "编码 JSON: %v\n", err)
		os.Exit(1)
	}
	raw = append(raw, '\n')
	if err := os.WriteFile(*out, raw, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "写入 %s: %v\n", *out, err)
		os.Exit(1)
	}
}
