# memory-console-be 的统一入口：本地、git 钩子、CI 都只调用这里的目标。
.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

MODULE    := github.com/hugegraph/MemOS/apps/memory-console-be
BIN       := bin
BIN_ABS   := $(abspath $(BIN))
GO        ?= go

# 开发工具版本。升级只改这里：stamp 文件名带版本，改了会自动重装到 ./bin。
GOLANGCI_LINT_VERSION := v2.15.0
GOVULNCHECK_VERSION   := v1.9.0
GOLANGCI_LINT := $(BIN)/golangci-lint
GOVULNCHECK   := $(BIN)/govulncheck
TOOLS_STAMP   := $(BIN)/.tools-$(GOLANGCI_LINT_VERSION)-$(GOVULNCHECK_VERSION)

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo none)
DATE    ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
# 控制台前端仓库位置。产物不入库，只在上镜像前拷到 web/dist；
# 未指定 MEMOSD_WEB_DIR 的部署只提供 API，不依赖这份产物。
WEB_CONSOLE_DIR ?= ../memory-console
# 聊天页的 agent runtime。pi 是 musl 静态二进制，sha256 固定；产物不入库。
PI_BIN ?= ../rust-pi/dist/pi-1.3.0-linux-amd64/pi
PI_SHA256 ?= c779edd6c3e0a0111e340008061cb76aa11eb949e9ddfdac40ed90e92e3b33ee
# scratch 镜像没有证书库；pi 走 HTTPS 时需要显式带一份 PEM bundle。
CA_BUNDLE ?= /etc/ssl/cert.pem
# 生产镜像仓库；`make image TAG=...` 用它拼完整引用。
IMAGE_REPO ?= example.invalid/example-project/memory-console
# 前端展示的 MCP 服务地址（构建期注入）。留空则开发环境按当前主机:8003 推。
# 前端页面上展示的 MCP 地址；不设会退化成「控制台主机:8002」，下线旧网关后那是错的。
WEB_SERVICE_BASE ?=
LDFLAGS := -s -w \
	-X $(MODULE)/internal/platform/version.Version=$(VERSION) \
	-X $(MODULE)/internal/platform/version.Commit=$(COMMIT) \
	-X $(MODULE)/internal/platform/version.Date=$(DATE)

.PHONY: help
help: ## 列出所有目标
	@awk 'BEGIN{FS=":.*##"; printf "用法: make <目标>\n\n"} /^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ 环境

.PHONY: tools
tools: $(TOOLS_STAMP) ## 安装固定版本的开发工具到 ./bin

$(TOOLS_STAMP):
	@mkdir -p $(BIN)
	GOBIN=$(BIN_ABS) $(GO) install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION)
	GOBIN=$(BIN_ABS) $(GO) install golang.org/x/vuln/cmd/govulncheck@$(GOVULNCHECK_VERSION)
	@rm -f $(BIN)/.tools-*
	@touch $@

.PHONY: hooks
hooks: ## 启用仓库自带的 git 钩子（core.hooksPath=.githooks，无需 Node）
	git config core.hooksPath .githooks
	chmod +x .githooks/*

##@ 质量

.PHONY: fmt
fmt: $(TOOLS_STAMP) ## 格式化（gofumpt + goimports）
	$(GOLANGCI_LINT) fmt ./...

.PHONY: fmt-check
fmt-check: $(TOOLS_STAMP) ## 只检查格式，不改文件
	@out="$$($(GOLANGCI_LINT) fmt --diff ./...)"; \
	if [ -n "$$out" ]; then echo "$$out"; echo "格式不符合规范，请执行 make fmt" >&2; exit 1; fi

.PHONY: lint
lint: $(TOOLS_STAMP) ## 全量静态检查
	$(GOLANGCI_LINT) run ./...

.PHONY: lint-new
lint-new: $(TOOLS_STAMP) ## 只检查相对 HEAD 新增或修改的代码（pre-commit 用）
	@if git rev-parse --verify -q HEAD >/dev/null; then \
		$(GOLANGCI_LINT) run --new-from-rev=HEAD ./...; \
	else \
		$(GOLANGCI_LINT) run ./...; \
	fi

.PHONY: vet
vet: ## go vet
	$(GO) vet ./...

.PHONY: test
test: ## 单元测试（-race、乱序）
	$(GO) test -race -shuffle=on -count=1 ./...

.PHONY: cover
cover: ## 覆盖率
	$(GO) test -race -covermode=atomic -coverprofile=coverage.out ./...
	$(GO) tool cover -func=coverage.out | tail -n 1

.PHONY: vuln
vuln: $(TOOLS_STAMP) ## 依赖漏洞扫描（需联网）
	$(GOVULNCHECK) ./...

.PHONY: tidy
tidy: ## 整理 go.mod
	$(GO) mod tidy

.PHONY: tidy-check
tidy-check: ## 检查 go.mod / go.sum 是否已 tidy
	$(GO) mod tidy -diff

.PHONY: docs-check
docs-check: ## 文档先行检查：模块文档完整、已索引，含代码的包都被认领
	@bash scripts/docs-check.sh

.PHONY: deploy-check
deploy-check: ## 检查公开容器配置
	python3 -I -B -m unittest discover -s scripts -p 'test_public_container.py'

# 前端仓库只消费 JSON；真源始终是本仓库的 YAML。用 go run 跑一个不进构建的
# 小工具派生，避免手写 JSON 与 YAML 漂移（目录名以 _ 开头，go list ./... 不看它）。
.PHONY: openapi-json
openapi-json: ## 把 api/openapi.dashboard.yaml 派生为前端用的 JSON（OUT=目标路径）
	@test -n "$(OUT)" || { echo "用法: make openapi-json OUT=<path>" >&2; exit 2; }
	$(GO) run ./scripts/_openapijson -in api/openapi.dashboard.yaml -out $(OUT)

.PHONY: check
check: fmt-check lint vet test tidy-check docs-check deploy-check build ## 完整检查（pre-push 与 CI 同款）

##@ 构建

.PHONY: build
build: ## 本机构建（CGO 关闭）
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN)/memosd ./cmd/memosd

.PHONY: build-linux
build-linux: ## 交叉编译 linux/amd64（集群节点）
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN)/memosd-linux-amd64 ./cmd/memosd

.PHONY: web
web: ## 构建控制台前端产物到 web/dist（需要 pnpm 与 Node >=20）
	@command -v pnpm >/dev/null || { echo "需要 pnpm（Node >=20）" >&2; exit 1; }
	@test -f $(WEB_CONSOLE_DIR)/package.json || { echo "找不到前端仓库: $(WEB_CONSOLE_DIR)（用 WEB_CONSOLE_DIR=... 指定）" >&2; exit 1; }
	cd $(WEB_CONSOLE_DIR) && pnpm install --frozen-lockfile && VITE_SERVICE_BASE=$(WEB_SERVICE_BASE) pnpm build
	rm -rf web/dist
	cp -R $(WEB_CONSOLE_DIR)/dist web/dist
	@test -f web/dist/index.html || { echo "前端产物缺少 index.html" >&2; exit 1; }
	@python3 -I scripts/frontend-meta.py $(WEB_CONSOLE_DIR) web/dist/frontend.json

.PHONY: pi
pi: ## 把 pi 运行时拷进 bin 并校验 sha256
	@test -f "$(PI_BIN)" || { echo "找不到 pi: $(PI_BIN)（用 PI_BIN=... 指定）" >&2; exit 1; }
	@mkdir -p $(BIN)
	@cp "$(PI_BIN)" $(BIN)/pi-linux-amd64
	@actual=$$(shasum -a 256 $(BIN)/pi-linux-amd64 | awk '{print $$1}'); \
	  if [ "$$actual" != "$(PI_SHA256)" ]; then \
	    echo "pi sha256 不匹配: $$actual（期望 $(PI_SHA256)）" >&2; exit 1; fi
	@echo "pi -> $(BIN)/pi-linux-amd64"

.PHONY: ca
ca: ## 把 CA bundle 拷进 bin（scratch 镜像里 pi 走 HTTPS 需要）
	@test -f "$(CA_BUNDLE)" || { echo "找不到 CA bundle: $(CA_BUNDLE)（用 CA_BUNDLE=... 指定）" >&2; exit 1; }
	@mkdir -p $(BIN)
	@cp "$(CA_BUNDLE)" $(BIN)/ca-certificates.crt
	@echo "ca -> $(BIN)/ca-certificates.crt"

.PHONY: image
# 依赖 build-linux、web 与 pi/ca：不重建就会出现「镜像 tag 是新的、产物是旧的」——
# bin/memosd-linux-amd64 是仓库里的产物，忘了重建时 Dockerfile 照样能把旧二进制 COPY 进去。
image: build-linux web pi ca ## 构建并推送生产镜像：make image TAG=prod-20261026-r32
	@test -n "$(TAG)" || { echo "需要 TAG=prod-<日期>-r<N>" >&2; exit 1; }
	@test -f web/dist/index.html || { echo "先跑 make web" >&2; exit 1; }
	@test -f web/dist/frontend.json || { echo "web/dist/frontend.json 缺失，重跑 make web" >&2; exit 1; }
	@test -f $(BIN)/pi-linux-amd64 || { echo "先跑 make pi" >&2; exit 1; }
	@test -f $(BIN)/ca-certificates.crt || { echo "先跑 make ca" >&2; exit 1; }
	@docker buildx build --platform linux/amd64 --target prod -f deploy/Dockerfile \
	  --build-arg BACKEND_REVISION=$$(git rev-parse --short HEAD) \
	  --build-arg FRONTEND_REVISION=$$(python3 -I -c 'import json;print(json.load(open("web/dist/frontend.json"))["commit"])') \
	  --build-arg FRONTEND_BRANCH=$$(python3 -I -c 'import json;print(json.load(open("web/dist/frontend.json"))["branch"])') \
	  -t $(IMAGE_REPO):$(TAG) --push .

.PHONY: clean
clean: ## 清理构建产物
	rm -rf $(BIN)/memosd $(BIN)/memosd-linux-amd64 coverage.out web/dist plugins

##@ git 钩子（由 .githooks/* 调用）

.PHONY: hook-pre-commit
hook-pre-commit: fmt-check lint-new

.PHONY: hook-pre-push
hook-pre-push: check
