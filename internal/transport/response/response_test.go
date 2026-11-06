package response

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// at 断言 JSON 解码后的动态值类型；类型不符直接失败，免得后面的下标越界掩盖真正的问题。
func at[T any](t *testing.T, v any) T {
	t.Helper()
	out, ok := v.(T)
	if !ok {
		t.Fatalf("值 %v 的类型是 %T，不是期望的 %T", v, v, out)
	}
	return out
}

func firstObject(t *testing.T, list any) map[string]any {
	t.Helper()
	items := at[[]any](t, list)
	if len(items) == 0 {
		t.Fatal("列表为空")
	}
	return at[map[string]any](t, items[0])
}

// wire 把 Encode 的输出解回通用结构：契约关心的是线上字段，而不是 Go 里的 map 形状。
func wire(t *testing.T, v any) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal([]byte(Encode(v)), &out); err != nil {
		t.Fatalf("Encode 的输出不是 JSON 对象: %v", err)
	}
	return out
}

func keys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func assertKeys(t *testing.T, got map[string]any, want ...string) {
	t.Helper()
	wantSet := make(map[string]bool, len(want))
	for _, k := range want {
		wantSet[k] = true
		if _, ok := got[k]; !ok {
			t.Errorf("缺少字段 %q，实际字段 %v", k, keys(got))
		}
	}
	for k := range got {
		if !wantSet[k] {
			t.Errorf("多出字段 %q", k)
		}
	}
}

func memory(id string) domain.Memory {
	return domain.Memory{
		ID: id, Key: "k-" + id, Content: "c-" + id, Subtype: "explicit_preference", Relativity: 0.5,
		CreatedAt: domain.UTCTime{Time: time.Date(2026, 10, 1, 8, 0, 0, 0, time.UTC)},
		Tags:      []string{"t"},
	}
}

func TestSearchFields(t *testing.T) {
	t.Parallel()
	result := domain.SearchResult{
		Facts:       []domain.Memory{memory("f1")},
		Preferences: []domain.Memory{memory("p1")},
		Tools:       []domain.Memory{memory("t1")},
		Dreams:      []domain.Memory{memory("d1")},
	}

	without := wire(t, Search(result, false))
	assertKeys(t, without, "memory_detail_list", "preference_detail_list", "tool_memory_detail_list")

	with := wire(t, Search(result, true))
	assertKeys(t, with, "memory_detail_list", "preference_detail_list", "tool_memory_detail_list", "dream_memory_detail_list")

	fact := firstObject(t, with["memory_detail_list"])
	assertKeys(t, fact, "memory_id", "memory_key", "memory_value", "relativity", "create_time", "tags")
	if fact["create_time"] != "2026-10-01T08:00:00Z" {
		t.Errorf("create_time = %v，期望 RFC3339 + Z", fact["create_time"])
	}
	pref := firstObject(t, with["preference_detail_list"])
	assertKeys(t, pref, "memory_id", "preference", "preference_type", "relativity", "create_time", "tags")
	tool := firstObject(t, with["tool_memory_detail_list"])
	assertKeys(t, tool, "memory_id", "tool_value", "relativity", "create_time", "tags")
	// dream 与事实同形。
	dream := firstObject(t, with["dream_memory_detail_list"])
	assertKeys(t, dream, "memory_id", "memory_key", "memory_value", "relativity", "create_time", "tags")
}

func TestSearchOmitsEmptyOptionalFields(t *testing.T) {
	t.Parallel()
	got := wire(t, Search(domain.SearchResult{Facts: []domain.Memory{{ID: "f1"}}}, false))
	fact := firstObject(t, got["memory_detail_list"])
	if _, ok := fact["create_time"]; ok {
		t.Error("零值时间不应输出 create_time")
	}
	if _, ok := fact["tags"]; ok {
		t.Error("空 tags 不应输出")
	}
	// 空结果仍是空数组，而不是 null，调用方可以直接遍历。
	for _, key := range []string{"preference_detail_list", "tool_memory_detail_list"} {
		list, ok := got[key].([]any)
		if !ok || len(list) != 0 {
			t.Errorf("%s = %v，期望空数组", key, got[key])
		}
	}
}

func TestProfileFields(t *testing.T) {
	t.Parallel()
	got := wire(t, Profile(domain.ProfileResult{
		Facts: []domain.Memory{memory("f1")}, Current: 2, Size: 10, Total: 31,
	}))
	assertKeys(t, got, "memory_detail_list", "preference_detail_list", "tool_memory_detail_list", "current", "size", "total")
	if got["current"] != float64(2) || got["size"] != float64(10) || got["total"] != float64(31) {
		t.Errorf("分页字段 = %v/%v/%v", got["current"], got["size"], got["total"])
	}
}

func TestAddDefaultsAndDeferred(t *testing.T) {
	t.Parallel()
	sync := wire(t, Add(domain.AddResult{}))
	assertKeys(t, sync, "status", "message")
	if sync["status"] != "success" || sync["message"] != "Memory added successfully" {
		t.Errorf("同步默认值 = %v", sync)
	}

	queued := wire(t, Add(domain.AddResult{
		Status: "success", Message: "Memory accepted.", Deferred: true, CallID: "job-1",
	}))
	assertKeys(t, queued, "status", "message", "deferred", "call_id")
	if !at[bool](t, queued["deferred"]) || queued["call_id"] != "job-1" || queued["message"] != "Memory accepted." {
		t.Errorf("异步结果 = %v", queued)
	}
}

func TestDeleteShapes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		in     domain.DeleteResult
		fields []string
		msg    string
	}{
		{
			"全部删除",
			domain.DeleteResult{DeletedIDs: []string{"a"}},
			[]string{"status", "deleted_ids", "message"},
			"Memories deleted successfully",
		},
		{
			"部分不存在",
			domain.DeleteResult{DeletedIDs: []string{"a"}, NotFoundIDs: []string{"b"}},
			[]string{"status", "deleted_ids", "not_found_ids", "message"},
			"Memories deleted successfully",
		},
		{
			"一条都没删时 not_found_ids 即使为空也给",
			domain.DeleteResult{},
			[]string{"status", "deleted_ids", "not_found_ids", "message"},
			"没有可删除的记忆（id 不存在或已删除）",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := wire(t, Delete(tc.in))
			assertKeys(t, got, tc.fields...)
			if got["status"] != "success" || got["message"] != tc.msg {
				t.Errorf("status/message = %v / %v", got["status"], got["message"])
			}
			if ids, ok := got["deleted_ids"].([]any); !ok || len(ids) != len(tc.in.DeletedIDs) {
				t.Errorf("deleted_ids = %v，期望数组且长度 %d", got["deleted_ids"], len(tc.in.DeletedIDs))
			}
		})
	}
}

func TestFeedbackShapes(t *testing.T) {
	t.Parallel()
	empty := wire(t, Feedback(domain.FeedbackResult{}))
	assertKeys(t, empty, "status", "message", "added", "updated")
	if empty["status"] != "success" || empty["message"] != "Feedback recorded" {
		t.Errorf("默认值 = %v", empty)
	}
	for _, key := range []string{"added", "updated"} {
		if list, ok := empty[key].([]any); !ok || len(list) != 0 {
			t.Errorf("%s = %v，期望空数组", key, empty[key])
		}
	}

	full := wire(t, Feedback(domain.FeedbackResult{
		Added:     []domain.FeedbackChange{{ID: "n1", Text: "new"}, {ID: "n2"}},
		Updated:   []domain.FeedbackChange{{ID: "u1", Text: "edited", ArchivedID: "old"}},
		Truncated: true,
	}))
	assertKeys(t, full, "status", "message", "added", "updated", "truncated")
	added := at[[]any](t, full["added"])
	if !reflect.DeepEqual(added[0], map[string]any{"id": "n1", "text": "new"}) ||
		!reflect.DeepEqual(added[1], map[string]any{"id": "n2"}) {
		t.Errorf("added = %v，空文本不应输出 text", added)
	}
	updated := at[[]any](t, full["updated"])[0]
	if !reflect.DeepEqual(updated, map[string]any{"id": "u1", "text": "edited", "archived_id": "old"}) {
		t.Errorf("updated = %v", updated)
	}
}

func TestEncode(t *testing.T) {
	t.Parallel()
	t.Run("去掉 nil 并用一个空格缩进", func(t *testing.T) {
		t.Parallel()
		got := Encode(map[string]any{
			"b":      1,
			"a":      "x",
			"gone":   nil,
			"nested": map[string]any{"keep": true, "drop": nil},
			"list":   []map[string]any{{"id": "1", "drop": nil}},
		})
		const want = `{
 "a": "x",
 "b": 1,
 "list": [
  {
   "id": "1"
  }
 ],
 "nested": {
  "keep": true
 }
}`
		if got != want {
			t.Fatalf("Encode =\n%s\n期望\n%s", got, want)
		}
	})
	t.Run("同一输入的输出稳定", func(t *testing.T) {
		t.Parallel()
		in := map[string]any{"z": 1, "y": 2, "x": 3, "w": 4}
		first := Encode(in)
		for range 20 {
			if Encode(in) != first {
				t.Fatal("map 键顺序不稳定，输出会随运行漂移")
			}
		}
	})
	t.Run("无法序列化时回退为空对象", func(t *testing.T) {
		t.Parallel()
		if got := Encode(map[string]any{"bad": make(chan int)}); got != "{}" {
			t.Fatalf("Encode = %q，期望 {}", got)
		}
	})
}
