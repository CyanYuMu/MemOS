/** 协议示例可能出现在代码里，代码中的标签必须原样保留。 */
function metadataOffset(text: string) {
  let fence = ''
  let offset = 0
  for (const line of text.split('\n')) {
    const delimiter = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1]
    if (delimiter) {
      if (!fence) fence = delimiter
      else if (delimiter[0] === fence[0] && delimiter.length >= fence.length) fence = ''
    } else if (!fence) {
      const visible = line.replace(/(`+)[\s\S]*?\1/g, (value) => ' '.repeat(value.length))
      const at = visible.indexOf('<graph-memory')
      if (at >= 0) return offset + at
    }
    offset += line.length + 1
  }
  return -1
}

/** 参考问题与正文分开，流式未完成的结构化尾部也不落到回答里。 */
export function parseAssistantResponse(text: string) {
  const at = metadataOffset(text)
  if (at < 0) {
    const start = text.lastIndexOf('<')
    const tail = text.slice(start)
    if (start >= 0 && tail.length >= 2 && ['<graph-memory-followups>', '<graph-memory-title>'].some((tag) => tag.startsWith(tail)))
      return { text: text.slice(0, start).trimEnd(), questions: [] as string[] }
    return { text, questions: [] as string[] }
  }
  const body = text.slice(0, at).trimEnd()
  const match = text.slice(at).match(/<graph-memory-followups>\s*([\s\S]*?)\s*<\/graph-memory-followups>/)
  if (!match) return { text: body, questions: [] as string[] }
  try {
    const value: unknown = JSON.parse(match[1])
    const questions = Array.isArray(value)
      ? [
          ...new Set(
            value
              .filter((q): q is string => typeof q === 'string')
              .map((q) => q.trim())
              .filter((q) => q && q.length <= 160),
          ),
        ].slice(0, 3)
      : []
    return { text: body, questions }
  } catch {
    return { text: body, questions: [] as string[] }
  }
}

/** 时间戳区分同文回答；缺失时保留历史绝对ID，正文只在本地参与摘要。 */
export async function answerKey(text: string, identity: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(identity + '\n' + text))
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}
