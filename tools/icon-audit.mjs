/** 图标对账：源码里引用的图标名 vs src/styles/icons.css 里定义的。
 *
 *  图标走 Iconify 的 CSS mask 模式，名字写错或没定义时不会报错，只是那一格空白。
 *  图标名可能单独出现，也可能拼在一长串 class 里，两种都要扫到。
 *
 *  用法: node tools/icon-audit.mjs
 */
import fs from 'node:fs'
import path from 'node:path'

const ROOT = process.cwd()
const CSS = path.join(ROOT, 'src', 'styles', 'icons.css')

/** icons.css 里 :where(.i-xxx){...} 的规则名 */
function defined() {
  const css = fs.readFileSync(CSS, 'utf8')
  const out = new Set()
  for (const m of css.matchAll(/:where\(\.((?:[^()\\]|\\.)+)\)/g)) out.add(m[1].replace(/\\(.)/g, '$1'))
  return out
}

function sources() {
  const files = []
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name)
      if (e.isDirectory()) walk(p)
      else if (/\.tsx?$/.test(e.name)) files.push(p)
    }
  }
  walk(path.join(ROOT, 'src'))
  return files
}

const D = defined()

// 图标名有两种写法：带冒号的 i-db:book，和带连字符的 i-db-book。
// 冒号式没有歧义；连字符式要靠已知的图标集前缀来界定，否则会和普通 class 混淆。
const SETS = [...new Set([...D].map((n) => n.match(/^i-([a-z0-9]+)[:-]/)?.[1]).filter(Boolean))]
const ICON = new RegExp(`\\bi-(?:[a-z0-9]+:[a-zA-Z0-9-]+|(?:${SETS.join('|')})-[a-zA-Z0-9-]+)`, 'g')

/** 源码里出现的图标名 → 出处 */
function used() {
  const out = new Map()
  for (const f of sources()) {
    const text = fs.readFileSync(f, 'utf8')
    for (const m of text.matchAll(ICON)) {
      const name = m[0]
      if (!out.has(name)) out.set(name, new Set())
      out.get(name).add(path.relative(ROOT, f))
    }
  }
  return out
}

const U = used()
const missing = [...U.keys()].filter((n) => !D.has(n)).sort()
const unused = [...D].filter((n) => !U.has(n)).sort()

console.log(`定义 ${D.size} 个，源码引用 ${U.size} 个（图标集：${SETS.join(' / ')}）`)
if (missing.length) {
  console.log('\n用了但没定义（会静默不显示）:')
  for (const n of missing) console.log(`  ${n}   ← ${[...U.get(n)].join(' ')}`)
} else {
  console.log('引用的图标全部有定义')
}
if (unused.length) console.log(`\n定义了但没引用（无害）: ${unused.join(' ')}`)
if (missing.length) process.exitCode = 1
