package upstream

import (
	"encoding/json"
	"testing"
)

func TestMemoryDetailsPreservesRealVersionsAndSources(t *testing.T) {
	t.Parallel()
	var meta map[string]any
	err := json.Unmarshal([]byte(`{"version":3,"history":["broken","{\"version\":1,\"memory\":\"old\",\"created_at\":\"2026-10-30T12:00:00Z\"}",{"version":2,"memory":"middle","timespec":{"valid_from":"2026-10-30T12:00:00Z","valid_to":"2026-10-31T00:00:00Z"}}],"sources":[{"type":"chat","role":"user","content":"original","chat_time":"2026-10-30T12:00:00Z"}]}`), &meta)
	if err != nil {
		t.Fatal(err)
	}
	version, history, sources := memoryDetails(meta)
	if version != 3 || len(history) != 2 || history[0].Version != 1 || history[0].Content != "old" || history[0].CreatedAt != "2026-10-30 20:00:00" || history[1].ValidTo != "2026-10-31 08:00:00" || len(sources) != 1 || sources[0].Content != "original" {
		t.Fatalf("version=%d history=%+v sources=%+v", version, history, sources)
	}
}
