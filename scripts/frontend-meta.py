#!/usr/bin/env python3
"""把前端仓库的身份写进 web/dist/frontend.json，随镜像一起发布。

发布时最常见的问题是「线上这份前端是哪次提交」，以前只能靠对比 asset hash 猜。
这里在构建前端之后落一个文件，后端启动时读进 /api/meta，镜像里也留一份：

    {"path": "../memory-console", "branch": "main", "commit": "0d4c45c8",
     "dirty": false, "builtAt": "2026-10-26T05:00:00Z"}

`path` 是相对后端仓库的路径（构建机上长什么样不重要，看的是这个相对关系）；
`dirty` 为 true 说明构建时前端工作区有未提交改动，镜像不可复现。

用法: python3 -I scripts/frontend-meta.py <前端目录> [输出文件]
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: frontend-meta.py <前端目录> [输出文件]", file=sys.stderr)
        return 2
    repo = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("web/dist/frontend.json")

    commit = git(repo, "rev-parse", "--short", "HEAD")
    if not commit:
        print(f"  {repo} 不是 git 仓库（或没有提交），前端版本记为 unknown", file=sys.stderr)
        commit = "unknown"

    meta = {
        "path": str(repo),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "commit": commit,
        # --quiet 返回非 0 表示有改动：这样的镜像不能复现，要能从版本信息里看出来。
        "dirty": subprocess.run(
            ["git", "-C", str(repo), "diff", "--quiet"], capture_output=True, check=False,
        ).returncode != 0,
        "builtAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  前端版本: {meta['branch']}@{meta['commit']}{'（有未提交改动）' if meta['dirty'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
