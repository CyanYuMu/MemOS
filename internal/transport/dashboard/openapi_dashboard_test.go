package dashboard

import (
	"context"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/getkin/kin-openapi/openapi3"
)

// TestOpenAPIDashboardContract 双向断言路由表与契约：实现了没写进 spec、spec 写了没实现，
// 都让测试失败。没有这条断言，控制台契约就只能靠人记着同步。
func TestOpenAPIDashboardContract(t *testing.T) {
	t.Parallel()

	doc, err := openapi3.NewLoader().LoadFromFile(
		filepath.Join("..", "..", "..", "api", "openapi.dashboard.yaml"))
	if err != nil {
		t.Fatalf("load openapi: %v", err)
	}
	if err := doc.Validate(context.Background()); err != nil {
		t.Fatalf("validate openapi: %v", err)
	}

	declared := make(map[string]struct{}, doc.Paths.Len())
	for path, item := range doc.Paths.Map() {
		for method := range item.Operations() {
			declared[normalizeRoute(method+" "+path)] = struct{}{}
		}
	}

	srv := newTestServer(t)
	implemented := make(map[string]struct{}, len(srv.routes))
	for _, pattern := range srv.routes {
		implemented[normalizeRoute(pattern)] = struct{}{}
	}
	// 两侧都空也会「通过」，先钉住规模。
	if len(declared) == 0 || len(implemented) == 0 {
		t.Fatalf("路由表与 spec 都应为非空：declared=%d implemented=%d", len(declared), len(implemented))
	}

	var undeclared, unimplemented []string
	for route := range implemented {
		if _, ok := declared[route]; !ok {
			undeclared = append(undeclared, route)
		}
	}
	for route := range declared {
		if _, ok := implemented[route]; !ok {
			unimplemented = append(unimplemented, route)
		}
	}
	sort.Strings(undeclared)
	sort.Strings(unimplemented)

	if len(undeclared) > 0 {
		t.Errorf("实现有、spec 没写（补 api/openapi.dashboard.yaml）：%v", undeclared)
	}
	if len(unimplemented) > 0 {
		t.Errorf("spec 有、实现没有（删掉，或补上 handler）：%v", unimplemented)
	}
}

var pathParam = regexp.MustCompile(`\{[^}]+\}`)

// normalizeRoute 抹平方法大小写与路径参数名：Go 的 {id} 和 spec 的 {apiKeyId} 是同一条路由。
func normalizeRoute(route string) string {
	method, path, ok := strings.Cut(strings.TrimSpace(route), " ")
	if !ok {
		return strings.ToUpper(strings.TrimSpace(route))
	}
	return strings.ToUpper(method) + " " + pathParam.ReplaceAllString(path, "{}")
}
