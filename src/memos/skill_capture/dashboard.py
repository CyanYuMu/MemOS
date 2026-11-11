"""Read-only, loopback-only status. No filesystem browsing or write endpoints."""

import html
import json
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def snapshot(store):
    with store.db() as db:
        sessions = [
            dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT 100")
        ]
        total = db.execute("SELECT count(*) FROM events").fetchone()[0]
        health = {r["id"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM health")}
    return {
        "enabled": store.config["enabled"],
        "projects": [p["name"] for p in store.config["projects"]],
        "sessions": sessions,
        "events": total,
        "experiences": store.experiences(),
        "health": health,
    }


def render(state):
    def escape(value):
        return html.escape(str(value), quote=True)

    rows = []
    names = {
        "synced": "Memory 回读已确认",
        "pending": "等待同步",
        "sending": "提交中／待核对",
        "pending_reconcile": "提交结果未知，等待回读",
        "invalid": "回读不一致，待处理",
    }
    for item in state["experiences"]:
        evidence = item["evidence"]
        rows.append(f"""<article><div class="row"><h2>{escape(item["project"])}</h2>
<span class="badge">{escape(names.get(item["status"], item["status"]))}</span></div>
<p class="muted">{escape(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["created"])))} · 会话 {escape(evidence["session"][:12])} · {len(evidence["events"])} 条事件</p>
<p>待核验经验 · 不具备 Skill 提炼／发布资格</p>
<p>完整性：{escape(", ".join(evidence["issues"]) or "未发现已支持事件的配对缺失")} · 结束信号：{escape(evidence["closed_by"] or "尚未结束")}</p>
<p>Memory ID：{escape(", ".join(item["memory_ids"]) or "尚未确认")}</p>
<p class="muted">{escape(item["reason"])}</p>
<details><summary>查看自动整理的经验</summary><pre>{escape(item["summary"])}</pre></details>
<details><summary>查看已采集事件与证据</summary><pre>{escape(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></details>
</article>""")
    last = state["health"].get("worker", {}).get("updated", 0)
    alive = time.time() - last < 90
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude 日常经验采集 · MemOS</title>
<style>body{{margin:0;background:#111827;color:#e5e7eb;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1080px;margin:40px auto;padding:0 24px}}h1{{font-size:28px}}h2{{font-size:19px;margin:0}}article,.panel{{background:#1f2937;border:1px solid #374151;border-radius:12px;padding:22px;margin:18px 0}}.row{{display:flex;justify-content:space-between;gap:16px}}.muted{{color:#9ca3af;font-size:14px}}.badge{{color:#93c5fd}}a{{color:#93c5fd}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}}summary{{cursor:pointer;padding:8px 0}}</style>
<main><div class="row"><h1>Claude 日常经验采集</h1><a href="/">刷新状态</a></div>
<p>在已接入项目中正常使用 Claude Code，新请求、工具结果和最终回复会自动进入本机账本，再同步到 Memory。</p>
<section class="panel"><b>采集：{"已启用" if state["enabled"] else "已停用"} · 后台：{"运行中" if alive else "启动中或心跳延迟"}</b>
<p>范围：{escape("、".join(state["projects"]))}</p>
<p>{len(state["sessions"])} 个会话（最近 100 个） · {state["events"]} 条事件 · {len(state["experiences"])} 份经验（最近 100 份）</p>
<p class="muted">只记录启用后的事件；不导入历史对话，不采集内部推理，不把 Agent 自报当作独立验收。没有自动发布 Skill。</p></section>
{"".join(rows) or "<article>等待你在已接入目录中新建 Claude Code 会话。</article>"}</main></html>"""


def server_for(store):
    port = store.config["port"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol
            # Reject DNS rebinding and cross-origin scripted reads; serve no CORS headers.
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"} or (
                self.headers.get("Sec-Fetch-Site") == "cross-site"
                and self.headers.get("Sec-Fetch-Mode") != "navigate"
            ):
                self.send_error(403)
                return
            if self.path not in {"/", "/status"}:
                self.send_error(404)
                return
            state = snapshot(store)
            body = (
                json.dumps(state, ensure_ascii=False) if self.path == "/status" else render(state)
            ).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
                if self.path == "/status"
                else "text/html; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
