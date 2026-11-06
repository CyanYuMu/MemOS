package domain

import (
	"crypto/rand"
	"encoding/hex"
)

// NewID 生成 16 字节随机 ID（hex），用于审计、任务与静态记录的确定性键。
func NewID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand 失败意味着系统熵源不可用，继续生成可预测 ID 更危险。
		panic("domain: crypto/rand unavailable: " + err.Error())
	}
	return hex.EncodeToString(buf)
}
