#!/usr/bin/env bash
# 提示词工作台的本地端到端环境：一条命令起控制面并播种账号与项目。
#
#   scripts/e2e-prompts-local.sh
#
# - 控制面固定 18102（console 角色），上游默认指向 http://127.0.0.1:18101，
#   即 dumemos 的 scripts/dev/prompt_service.py。上游没起也能启动，只是接口会 503。
# - 账号与项目只写在临时目录的 SQLite 里，退出时删掉；不碰任何真实库、集群或 PVC。
# - 环境起好后停在前台，Ctrl-C 结束并清理；PROMPT_E2E_EXIT_AFTER_SMOKE=1 时
#   跑完冒烟请求就退出（给自动化用）。
#
# 可覆盖的环境变量：PROMPT_E2E_CONSOLE_PORT、PROMPT_UPSTREAM_URL、
# PROMPT_E2E_PROJECT、PROMPT_E2E_EDITOR、PROMPT_E2E_VIEWER、PROMPT_E2E_PASSWORD、
# PROMPT_E2E_ADMIN_TOKEN。
set -euo pipefail
cd "$(dirname "$0")/.."

CONSOLE_PORT="${PROMPT_E2E_CONSOLE_PORT:-18102}"
UPSTREAM_URL="${PROMPT_UPSTREAM_URL:-http://127.0.0.1:18101}"
PROJECT="${PROMPT_E2E_PROJECT:-prompt-e2e}"
EDITOR_USER="${PROMPT_E2E_EDITOR:-prompt-editor}"
VIEWER_USER="${PROMPT_E2E_VIEWER:-prompt-viewer}"
PASSWORD="${PROMPT_E2E_PASSWORD:-prompts-e2e-2026}"
ADMIN_TOKEN="${PROMPT_E2E_ADMIN_TOKEN:-prompts-e2e-admin-token}"
ADMIN_USER="prompts-e2e-admin"
ADMIN_PASSWORD="prompts-e2e-admin-2026"
BASE="http://127.0.0.1:${CONSOLE_PORT}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/memosd-prompts.XXXXXX")"
PID=""

cleanup() {
	status=$?
	if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
		kill "$PID" 2>/dev/null || true
		wait "$PID" 2>/dev/null || true
	fi
	if [ "$status" -ne 0 ] && [ -f "$TMP/memosd.log" ]; then
		echo "--- memosd.log（最后 40 行）---" >&2
		tail -n 40 "$TMP/memosd.log" >&2
	fi
	rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

fail() {
	echo "失败：$*" >&2
	exit 1
}

# admin_call 用管理 Token 调控制面，把状态码写到 stdout、响应体留在 $TMP/resp.json。
admin_call() {
	local method=$1 path=$2 body=${3:-'{}'}
	curl -sS -o "$TMP/resp.json" -w '%{http_code}' \
		-X "$method" -H "Authorization: Bearer ${ADMIN_TOKEN}" \
		-H 'Content-Type: application/json' -d "$body" "${BASE}${path}"
}

echo "[1/4] 构建 memosd（临时目录，不入库）"
go build -o "$TMP/memosd" ./cmd/memosd

# 端口上已经有服务时直接报错：否则就绪探测会打到上一次没清理干净的进程，
# 播种数据也写进那个进程的旧库，排查起来像“数据凭空存在”。
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
	fail "端口 ${CONSOLE_PORT} 已有服务在监听；先停掉它（可能是上次没清理的 memosd）"
fi

echo "[2/4] 启动控制面 ${BASE}（console 角色，上游 ${UPSTREAM_URL}）"
env \
	MEMOSD_ROLE=console \
	MEMOSD_CONSOLE_ADDR="127.0.0.1:${CONSOLE_PORT}" \
	MEMOSD_MCP_ADDR="127.0.0.1:$((CONSOLE_PORT + 1))" \
	MEMOSD_DB_PATH="$TMP/memosd.db" \
	MEMOS_API_BASE_URL="$UPSTREAM_URL" \
	MEMOSD_ADMIN_TOKEN="$ADMIN_TOKEN" \
	MEMOSD_ADMIN_USER="$ADMIN_USER" \
	MEMOSD_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
	MEMOSD_COOKIE_SECURE=false \
	MEMOSD_LOG_LEVEL=info \
	MEMOSD_LOG_FORMAT=text \
	"$TMP/memosd" serve >"$TMP/memosd.log" 2>&1 &
PID=$!

ready=0
for _ in $(seq 1 150); do
	if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then
		ready=1
		break
	fi
	kill -0 "$PID" 2>/dev/null || fail "memosd 已退出（端口 ${CONSOLE_PORT} 可能被占用）"
	sleep 0.1
done
[ "$ready" = 1 ] || fail "控制面 15 秒内没有就绪"

echo "[3/4] 播种账号与项目 ${PROJECT}"
create_account() {
	local user=$1 code
	code=$(admin_call POST /admin/accounts "{\"username\":\"${user}\",\"password\":\"${PASSWORD}\"}")
	case "$code" in
	200 | 201) ;;
	*) fail "创建账号 ${user}: HTTP ${code} $(cat "$TMP/resp.json")" ;;
	esac
}
create_account "$EDITOR_USER"
create_account "$VIEWER_USER"

# 管理 Token 没有账号身份，建项目时显式把 editor 落成 owner。
code=$(admin_call POST /api/dashboard/project/create \
	"{\"name\":\"${PROJECT}\",\"description\":\"提示词工作台联调\",\"ownerUsername\":\"${EDITOR_USER}\"}")
case "$code" in
200) ;;
*) fail "创建项目 ${PROJECT}: HTTP ${code} $(cat "$TMP/resp.json")" ;;
esac

code=$(admin_call POST "/admin/tenants/${PROJECT}/members" "{\"username\":\"${VIEWER_USER}\",\"role\":\"viewer\"}")
case "$code" in
200) ;;
*) fail "把 ${VIEWER_USER} 加入项目: HTTP ${code} $(cat "$TMP/resp.json")" ;;
esac

echo "[4/4] 冒烟：用 editor 会话调 /api/dashboard/prompts/list"
EDITOR_COOKIES="$TMP/editor.cookies"
login_code=$(curl -sS -o "$TMP/login.json" -w '%{http_code}' -c "$EDITOR_COOKIES" \
	-H 'Content-Type: application/json' \
	-d "{\"username\":\"${EDITOR_USER}\",\"password\":\"${PASSWORD}\"}" "${BASE}/__login")
[ "$login_code" = 200 ] || fail "editor 登录: HTTP ${login_code} $(cat "$TMP/login.json")"

list_code=$(curl -sS -o "$TMP/list.json" -w '%{http_code}' -b "$EDITOR_COOKIES" \
	-H 'Content-Type: application/json' -d "{\"projectId\":\"${PROJECT}\"}" \
	"${BASE}/api/dashboard/prompts/list")
echo "  prompts/list -> HTTP ${list_code} $(head -c 240 "$TMP/list.json")"
if [ "$list_code" != 200 ]; then
	echo "  （上游 ${UPSTREAM_URL} 未就绪时这里是 503；先起 dumemos 的 prompt_service.py 再试）"
fi

cat <<EOF

环境已就绪
==========
控制面        ${BASE}
上游          ${UPSTREAM_URL}
项目          ${PROJECT}
editor        ${EDITOR_USER} / ${PASSWORD}   （可编辑、试跑、改写）
viewer        ${VIEWER_USER} / ${PASSWORD}   （只读：三条读路由可用，写路由 403）
平台管理员    ${ADMIN_USER} / ${ADMIN_PASSWORD}
管理 Token    ${ADMIN_TOKEN}

登录（浏览器打开 ${BASE}/__login，或 curl 存 cookie）：
  curl -sS -c /tmp/editor.cookies -H 'Content-Type: application/json' \\
    -d '{"username":"${EDITOR_USER}","password":"${PASSWORD}"}' ${BASE}/__login

调用示例：
  curl -sS -b /tmp/editor.cookies -H 'Content-Type: application/json' \\
    -d '{"projectId":"${PROJECT}"}' ${BASE}/api/dashboard/prompts/list
  curl -sS -b /tmp/editor.cookies -H 'Content-Type: application/json' \\
    -d '{"projectId":"${PROJECT}","key":"extract.chat"}' ${BASE}/api/dashboard/prompts/detail

状态数据只在临时目录，Ctrl-C 后连同进程一起清理。
EOF

if [ "${PROMPT_E2E_EXIT_AFTER_SMOKE:-0}" = "1" ]; then
	exit 0
fi

echo "按 Ctrl-C 停止"
wait "$PID"
