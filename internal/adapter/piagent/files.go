package piagent

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// agentDirs 是由服务端生成的会话目录，不拼接任何用户输入。
type agentDirs struct {
	agent    string
	sessions string
	cwd      string
	// runtime 放本服务自己的文件：会话锁、历史快照、受理队列。与 pi 的
	// sessions 分开，避免 pi 扫描会话目录时把我们的文件当成会话。
	runtime string
}

func sessionDirs(root, tenantID, accountID string) agentDirs {
	base := filepath.Join(root, hash16(tenantID), hash16(accountID))
	return agentDirs{
		agent:    filepath.Join(base, "agent"),
		sessions: filepath.Join(base, "sessions"),
		cwd:      filepath.Join(base, "cwd"),
		runtime:  filepath.Join(base, "runtime"),
	}
}

// dirsForSpec 保留旧会话路径，新会话的所有运行文件都放在独立目录。
func dirsForSpec(root string, spec domain.ChatSessionSpec) agentDirs {
	if spec.ConversationID == "" {
		return sessionDirs(root, spec.TenantID, spec.AccountID)
	}
	base := filepath.Join(root, hash16(spec.TenantID), hash16(spec.AccountID), "conversations", hash16(spec.ConversationID))
	return agentDirs{agent: filepath.Join(base, "agent"), sessions: filepath.Join(base, "sessions"), cwd: filepath.Join(base, "cwd"), runtime: filepath.Join(base, "runtime")}
}

func hash16(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])[:16]
}

// writeModelsConfig 写 BYOK provider。apiKey 用 ${PI_BYOK_KEY} 引用，明文只经
// 进程环境传递，不落在文件里可被同 UID 的其他进程直接读到的位置。
func writeModelsConfig(agentDir string, spec domain.ChatSessionSpec, opts Options) error {
	doc := map[string]any{
		"providers": map[string]any{
			//nolint:gosec // apiKey 写的是 ${PI_BYOK_KEY} 占位符，明文只经环境注入
			"byok": map[string]any{
				"baseUrl": spec.BaseURL,
				"apiKey":  "${PI_BYOK_KEY}",
				"api":     "openai-responses",
				"models": []any{
					map[string]any{
						"id":            spec.Model,
						"contextWindow": opts.ContextWindow,
						"maxTokens":     opts.MaxTokens,
					},
				},
			},
		},
	}
	body, err := json.Marshal(doc)
	if err != nil {
		return fmt.Errorf("piagent: encode models.json: %w", err)
	}
	return writeFileAtomic(filepath.Join(agentDir, "models.json"), body)
}

// AllMCPTools 是 MCP 侧实现的全部记忆工具，也是 MEMOSD_CHAT_TOOLS 的合法取值集合。
var AllMCPTools = []string{"add_message", "search_memory", "update_memory", "delete_memory", "add_feedback", "get_user_profile"}

// DefaultMCPTools 让个人助手支持真实记忆读写；删改只接受用户明确指令。
// 服务层仍逐条核验租户归属，部署可用 MEMOSD_CHAT_TOOLS 收紧白名单。
var DefaultMCPTools = []string{"search_memory", "add_message", "get_user_profile", "update_memory", "delete_memory", "add_feedback"}

// writeMCPConfig 让 pi 通过同进程 MCP 拿记忆工具。Authorization 与自定义审计头
// 都用 ${ENV} 引用，token 只经 spawn env 注入。
func writeMCPConfig(agentDir, mcpURL, projectKey string, tools []string) error {
	if projectKey == "" {
		return errors.New("piagent: project key 不能为空")
	}
	exposure := make(map[string]string, len(AllMCPTools))
	for _, name := range AllMCPTools {
		exposure[name] = "hidden"
	}
	for _, name := range tools {
		exposure[name] = "direct"
	}
	doc := map[string]any{
		"autoEnableCodemode": false,
		"mcpServers": map[string]any{
			"memory-console": map[string]any{
				"type": "streamable-http",
				"url":  mcpURL,
				// 这组工具数量少，直接声明，避免模型先发现工具；每个操作保留独立执行事件。
				"exposure":     "direct",
				"toolExposure": exposure,
				"headers": map[string]string{
					"Authorization":              "Bearer ${MEMOS_MCP_TOKEN}",
					"X-Memos-Source":             "dashboard-chat-agent",
					"X-GraphMemory-Chat-Session": "${GRAPH_MEMORY_CHAT_SESSION_ID}",
				},
			},
		},
	}
	body, err := json.Marshal(doc)
	if err != nil {
		return fmt.Errorf("piagent: encode mcp.json: %w", err)
	}
	return writeFileAtomic(filepath.Join(agentDir, "mcp.json"), body)
}

// writeFileAtomic 先写同目录临时文件再 rename，避免 pi 读到半份 JSON。
func writeFileAtomic(path string, body []byte) error {
	dir := filepath.Dir(path)
	f, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return fmt.Errorf("piagent: create temp file: %w", err)
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }()
	if err := f.Chmod(0o600); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: chmod temp file: %w", err)
	}
	if _, err := f.Write(body); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: write temp file: %w", err)
	}
	// 落盘后再 rename：受理队列与历史快照要能扛住进程被杀。
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: sync temp file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("piagent: close temp file: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("piagent: replace %s: %w", filepath.Base(path), err)
	}
	// rename 的原子性只在内存里；目录项本身要 fsync 才落盘，否则掉电后可能
	// 看不到这个文件（进程被杀不受影响）。
	if d, err := os.Open(dir); err == nil {
		_ = d.Sync()
		_ = d.Close()
	}
	return nil
}

func knownTool(name string) bool {
	return slices.Contains(AllMCPTools, name)
}
