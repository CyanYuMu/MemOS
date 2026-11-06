package audit

import (
	"sort"
	"strconv"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// recordBytes 估算一条记录占用内存的字节数；只求量级正确，不做精确序列化。
func recordBytes(rec domain.AuditRecord) int {
	size := 256 + len(rec.Tool) + len(rec.TenantID) + len(rec.UserID) +
		len(rec.SessionID) + len(rec.Err) + len(rec.Query) + len(rec.Output) +
		len(rec.RequestID) + len(rec.RemoteIP)
	return size + valueBytes(rec.Input, 0)
}

func valueBytes(v any, depth int) int {
	if depth > 16 {
		return 64 // 超深嵌套按固定成本计，避免恶意结构拖慢估算
	}
	switch typed := v.(type) {
	case nil:
		return 8
	case string:
		return len(typed) + 8
	case bool, float64, int, int64, int32, uint64:
		return 16
	case []any:
		n := 16
		for _, item := range typed {
			n += valueBytes(item, depth+1)
		}
		return n
	case map[string]any:
		n := 16
		for key, item := range typed {
			n += len(key) + 8 + valueBytes(item, depth+1)
		}
		return n
	default:
		return 32
	}
}

// truncationMarker 是截断标记。保留标记而不是静默截断：审计用于事后审查，
// 阅读者必须能一眼看出正文不完整、被截掉了多少字节，否则会把残缺内容当成原始事实。
func truncationMarker(removed int) string {
	return "\n…[audit payload truncated: " + strconv.Itoa(removed) + " bytes]"
}

// capPayload 把 input/output/query 的估算总量压到 maxAuditPayloadBytes 以内。
// 三个字段按各自大小比例分配额度并保留 1 KiB 最小份额，避免一个超大 output
// 把 input 与 query 挤成空串；额度用尽时按 UTF-8 边界截断并追加标记。
func capPayload(rec domain.AuditRecord) domain.AuditRecord {
	inBytes := valueBytes(rec.Input, 0)
	outBytes := len(rec.Output)
	queryBytes := len(rec.Query)
	if inBytes+outBytes+queryBytes <= maxAuditPayloadBytes {
		return rec
	}
	budgets := payloadBudgets([3]int{inBytes, outBytes, queryBytes}, maxAuditPayloadBytes)
	if inBytes > budgets[0] {
		rec.Input = truncateInput(rec.Input, budgets[0])
	}
	if outBytes > budgets[1] {
		rec.Output = truncateText(rec.Output, budgets[1])
	}
	if queryBytes > budgets[2] {
		rec.Query = truncateText(rec.Query, budgets[2])
	}
	return rec
}

// payloadBudgets 按字段大小比例切分总量上限。先给非空字段各留 1 KiB
// （足够放下截断标记），剩余额度再按“超出部分”的比例分配；小字段原样保留。
func payloadBudgets(sizes [3]int, limit int) [3]int {
	var total int
	for _, size := range sizes {
		total += size
	}
	if total <= limit {
		return sizes
	}
	const minShare = 1 << 10
	var (
		budgets [3]int
		used    int
		excess  int
	)
	for i, size := range sizes {
		if size <= 0 {
			continue
		}
		share := min(size, minShare)
		budgets[i] = share
		used += share
		excess += size - share
	}
	remaining := max(limit-used, 0)
	for i, size := range sizes {
		over := size - budgets[i]
		if over <= 0 || excess <= 0 {
			continue
		}
		add := remaining * over / excess
		budgets[i] += min(add, over)
	}
	return budgets
}

// truncateText 在 limit 字节内保留 s 的前缀，保证不切断 UTF-8 字符，
// 并把删除的字节数写进末尾标记。返回值的长度（含标记）不超过 limit。
func truncateText(s string, limit int) string {
	if limit >= len(s) {
		return s
	}
	if limit <= 0 {
		return ""
	}
	keep := limit
	for range 8 {
		removed := len(s) - keep
		marker := truncationMarker(removed)
		next := max(limit-len(marker), 0)
		next = utf8Cut(s, next)
		if next == keep {
			break
		}
		keep = next
	}
	marker := truncationMarker(len(s) - keep)
	// 标记的十进制位数可能在上一步之后才变化；回退到能同时放下正文与标记的位置。
	for keep > 0 && keep+len(marker) > limit {
		keep = utf8Cut(s, keep-1)
		marker = truncationMarker(len(s) - keep)
	}
	if keep+len(marker) > limit {
		// 额度连标记都放不下：退化为按 UTF-8 边界的硬截断，至少不能产生非法 UTF-8。
		return s[:utf8Cut(s, limit)]
	}
	return s[:keep] + marker
}

// utf8Cut 把字节下标回退到 UTF-8 字符起始处，避免截断出半个 rune。
func utf8Cut(s string, n int) int {
	if n <= 0 {
		return 0
	}
	if n >= len(s) {
		return len(s)
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return n
}

// truncateInput 在估算字节额度内递归复制 input。字符串按 UTF-8 边界截断并追加
// 标记；额度不够时整项丢弃，而不是留下没有标记的残缺值。复制也避免改写调用方
// 传入的 map（调用方可能在别处复用同一份请求体）。
func truncateInput(input map[string]any, limit int) map[string]any {
	if len(input) == 0 || limit <= 0 {
		return nil
	}
	budget := limit
	value, ok := truncateValue(input, &budget, 0)
	if !ok {
		return nil
	}
	out, ok := value.(map[string]any)
	if !ok {
		return nil
	}
	return out
}

func truncateValue(value any, budget *int, depth int) (any, bool) {
	if depth > 16 {
		// 与 valueBytes 的深度护栏一致：过深的结构不再保留，避免恶意嵌套。
		return nil, false
	}
	switch typed := value.(type) {
	case nil:
		return takeFixedCost(budget, 8, nil)
	case string:
		return truncateStringValue(typed, budget)
	case bool, float64, int, int64, int32, uint64:
		return takeFixedCost(budget, 16, typed)
	case []any:
		return truncateSliceValue(typed, budget, depth)
	case map[string]any:
		return truncateMapValue(typed, budget, depth)
	default:
		return takeFixedCost(budget, 32, value)
	}
}

func takeFixedCost(budget *int, cost int, value any) (any, bool) {
	if *budget < cost {
		return nil, false
	}
	*budget -= cost
	return value, true
}

func truncateStringValue(value string, budget *int) (any, bool) {
	if *budget < 8 {
		return nil, false
	}
	avail := *budget - 8
	if len(value) > avail {
		if avail < len(truncationMarker(len(value))) {
			return nil, false
		}
		value = truncateText(value, avail)
	}
	*budget -= len(value) + 8
	return value, true
}

func truncateSliceValue(items []any, budget *int, depth int) (any, bool) {
	if *budget < 16 {
		return nil, false
	}
	*budget -= 16
	out := make([]any, 0, len(items))
	for _, item := range items {
		if converted, ok := truncateValue(item, budget, depth+1); ok {
			out = append(out, converted)
		}
	}
	return out, true
}

func truncateMapValue(input map[string]any, budget *int, depth int) (any, bool) {
	if *budget < 16 {
		return nil, false
	}
	*budget -= 16
	type keySize struct {
		key  string
		size int
	}
	items := make([]keySize, 0, len(input))
	for key, value := range input {
		items = append(items, keySize{key: key, size: valueBytes(value, depth+1)})
	}
	// 小值先处理：超大字符串不能把同一层的短字段（计数、开关、ID）挤掉。
	// 相同大小时按 key 排序，保证同一份 input 每次截断结果一致。
	sort.Slice(items, func(i, j int) bool {
		if items[i].size != items[j].size {
			return items[i].size < items[j].size
		}
		return items[i].key < items[j].key
	})
	out := make(map[string]any, len(input))
	for _, item := range items {
		cost := len(item.key) + 8
		if *budget < cost {
			continue
		}
		*budget -= cost
		converted, ok := truncateValue(input[item.key], budget, depth+1)
		if !ok {
			continue
		}
		out[item.key] = converted
	}
	return out, true
}
