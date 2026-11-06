/** 复制到剪贴板。
 *
 *  ⚠️ **必须有 `execCommand` 兜底**：控制台的部署形态是 `http://<内网 IP>:8280`，
 *  它不是 secure context，而 `navigator.clipboard` 只在 secure context（https / localhost）
 *  上存在——在部署实例上它是 **`undefined`**（2026-10-19 实测
 *  `isSecureContext=false`、`typeof navigator.clipboard === 'undefined'`）。
 *  原来四个页面直接调 `navigator.clipboard.writeText(...)`，点「复制」必然抛异常，
 *  用户看到的是「请求失败：剪贴板不可用」，而这句话还把锅甩给了网络请求。
 *
 *  ⚠️ 兜底那条路（临时 textarea + `execCommand('copy')`）已被标记为废弃，但它仍是
 *  非安全上下文里**唯一**还能用的办法，主流浏览器都还支持。所以先试新 API、失败再兜底，
 *  两条都不行才报错——不要一上来就用兜底，安全上下文里新 API 更可靠（不受焦点/选区影响）。
 */

/** 同步兜底：临时 textarea + execCommand('copy')。返回是否成功。 */
function copyViaExecCommand(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  // ⚠️ 放到视口外，**不能**用 `display:none` / `visibility:hidden`：
  //    隐藏元素选不中，`execCommand('copy')` 会直接返回 false。
  ta.style.position = 'fixed'
  ta.style.top = '0'
  ta.style.left = '-9999px'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  try {
    ta.select()
    // iOS Safari 上 `select()` 对 textarea 不生效，要再设一次选区
    ta.setSelectionRange(0, text.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(ta)
  }
}

/** 复制文本。成功返回，失败抛错（调用方负责提示）。 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // 落到兜底：非安全上下文、页面失焦、权限被拒都会走到这里
    }
  }
  if (!copyViaExecCommand(text)) throw new Error('浏览器不允许复制')
}
