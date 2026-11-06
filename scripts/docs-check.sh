#!/usr/bin/env bash
# 文档先行检查：模块文档完整、已列入索引，且每个含代码的包都被 accepted/implemented 的文档认领。
set -eu
cd "$(dirname "$0")/.."

dir=docs/module
fail=0
err() {
	echo "docs-check: $*" >&2
	fail=1
}

claims=$(mktemp)
trap 'rm -f "$claims"' EXIT

front() {
	awk 'NR==1 && $0!="---" {exit} NR>1 && $0=="---" {exit} NR>1 {print}' "$1"
}

for f in "$dir"/*.md; do
	base=$(basename "$f")
	case "$base" in README.md | _template.md) continue ;; esac

	fm=$(front "$f")
	name=$(printf '%s\n' "$fm" | sed -n 's/^module: *//p')
	status=$(printf '%s\n' "$fm" | sed -n 's/^status: *//p')

	[ -n "$name" ] || err "$f: front matter 缺少 module"
	[ "$name.md" = "$base" ] || err "$f: 文件名应为 $name.md"
	case "$status" in
	draft | accepted | implemented) ;;
	*) err "$f: status 必须是 draft、accepted 或 implemented" ;;
	esac
	grep -qF "($base)" "$dir/README.md" || err "$f: 未列入 $dir/README.md 的索引"

	for h in "## 职责与边界" "## 依赖" "## 契约" "## 错误与边界情况" "## 测试与验证" "## 参考来源" "## 实现记录"; do
		grep -qF "$h" "$f" || err "$f: 缺少章节「${h}」"
	done

	for p in $(printf '%s\n' "$fm" | sed -n 's/^  - *//p'); do
		printf '%s %s %s\n' "$p" "$status" "$base" >>"$claims"
	done
done

for link in $(grep -Eo '\]\([a-zA-Z0-9_-]+\.md\)' "$dir/README.md" | tr -d '])(['); do
	[ -f "$dir/$link" ] || err "$dir/README.md: 索引指向不存在的 $link"
done

for d in $(find cmd internal -type d | sort); do
	has_code=$(find "$d" -maxdepth 1 -type f -name '*.go' ! -name 'doc.go' | head -n 1)
	[ -n "$has_code" ] || continue
	row=$(awk -v p="$d" '$1 == p {print; exit}' "$claims")
	if [ -z "$row" ]; then
		err "$d 含代码，但没有任何模块文档在 packages 里认领它"
		continue
	fi
	case "$row" in
	*" draft "*) err "$d 的模块文档仍是 draft，先把文档改成 accepted 再写代码" ;;
	esac
done

[ "$fail" -eq 0 ] && echo "docs-check: ok"
exit "$fail"
