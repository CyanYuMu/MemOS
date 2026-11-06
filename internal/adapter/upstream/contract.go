package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ContractVersion 是控制面理解的上游契约版本（上游 src/memos/api/contract.py）。
// 上游以 GET /product/contract 公布；版本不一致时控制面不接流量，而不是靠猜字段含义。
const ContractVersion = 2

// contractTimeout 是自检的超时：就绪探针本身只给 3s，这里必须更短并失败即返回。
const contractTimeout = 2 * time.Second

// Contract 是上游公布的契约摘要。只解析控制面真正依赖的字段，新增字段不影响解析。
type Contract struct {
	ContractVersion int `json:"contract_version"`
	Time            struct {
		Basis string `json:"basis"`
	} `json:"time"`
	Sources struct {
		Default string `json:"default"`
	} `json:"sources"`
	TopKMax int `json:"top_k_max"`
}

// Contract 读取上游的契约声明。上游没实现该端点（旧镜像）时返回错误，
// 由调用方决定是否放流量。
func (c *Client) Contract(ctx context.Context) (Contract, error) {
	ctx, cancel := context.WithTimeout(ctx, contractTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/product/contract", nil)
	if err != nil {
		return Contract{}, fmt.Errorf("upstream contract: %w", err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return Contract{}, fmt.Errorf("upstream contract: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	// 没有契约端点就是上游版本不对：旧镜像（Node BFF + Python 网关）2026-10-25 已经
	// 全部下线，不存在需要放行的旧上游。宁可 readyz 直接不健康，也不要靠猜字段含义
	// 继续接流量——之前放行过一段时间，结果是版本/时间口径不一致时没人发现。
	if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusMethodNotAllowed {
		return Contract{}, fmt.Errorf("upstream contract: 端点不存在（HTTP %d），上游未实现契约自检", resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return Contract{}, fmt.Errorf("upstream contract: HTTP %d", resp.StatusCode)
	}
	var contract Contract
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&contract); err != nil {
		return Contract{}, fmt.Errorf("upstream contract: decode: %w", err)
	}
	return contract, nil
}

// CheckContract 校验上游契约与控制面一致，供 /readyz 使用。
// 失败原因要足够具体，便于区分“上游旧镜像没这个端点”“版本不匹配”“只读字段口径变了”。
func (c *Client) CheckContract(ctx context.Context) error {
	contract, err := c.Contract(ctx)
	if err != nil {
		return err
	}
	if contract.ContractVersion != ContractVersion {
		return fmt.Errorf("upstream contract version %d, control plane expects %d", contract.ContractVersion, ContractVersion)
	}
	if contract.Time.Basis != "utc" {
		return fmt.Errorf("upstream time basis %q, control plane expects utc", contract.Time.Basis)
	}
	if contract.Sources.Default != "omitted" {
		return fmt.Errorf("upstream sources default %q, control plane expects omitted", contract.Sources.Default)
	}
	if contract.TopKMax != domain.ContractTopKMax {
		return fmt.Errorf("upstream top_k_max %d, control plane expects %d", contract.TopKMax, domain.ContractTopKMax)
	}
	return nil
}
