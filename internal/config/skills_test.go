package config

import (
	"strings"
	"testing"
)

func TestSkillServiceConfiguration(t *testing.T) {
	for _, vars := range []map[string]string{
		{"MEMOS_SKILL_API_BASE_URL": "http://127.0.0.1:8100", "MEMOS_SKILL_API_TOKEN": "too-short"},
		{"MEMOS_SKILL_API_BASE_URL": "http://127.0.0.1:8100"},
		{"MEMOS_SKILL_API_TOKEN": "secret"},
		{"MEMOS_SKILL_API_BASE_URL": "file:///tmp/skills", "MEMOS_SKILL_API_TOKEN": "secret"},
		{"MEMOS_SKILL_API_BASE_URL": "http://user:pass@host", "MEMOS_SKILL_API_TOKEN": "secret"},
		{"MEMOS_SKILL_API_BASE_URL": "http://host/?token=secret", "MEMOS_SKILL_API_TOKEN": "secret"},
		{"MEMOS_SKILL_API_BASE_URL": "http://host", "MEMOS_SKILL_API_TOKEN": "secret\r\nX: value"},
	} {
		if _, err := FromEnv(envFrom(vars)); err == nil {
			t.Fatalf("invalid Skill configuration accepted")
		}
	}
	cfg, err := FromEnv(envFrom(map[string]string{"MEMOS_SKILL_API_BASE_URL": "http://127.0.0.1:8100/", "MEMOS_SKILL_API_TOKEN": "service-only-secret-at-least-32-characters"}))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Skill.BaseURL != "http://127.0.0.1:8100" || cfg.Skill.Token != "service-only-secret-at-least-32-characters" {
		t.Fatal("Skill config not injected")
	}
	if strings.Contains(cfg.Redacted(), "service-only-secret-at-least-32-characters") {
		t.Fatal("service credential leaked")
	}
}
