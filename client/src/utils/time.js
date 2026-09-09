// 统一时间格式化（2026-09-09）
//
// 后端 SQLite 存的是无时区标记的 UTC 时间（"2026-09-09 12:38:44"），裸 new Date()
// 会按本地时间解析，显示差一个时区偏移；ISO 带偏移的字符串（如 attempts_log 的
// "2026-09-09T12:37:38+00:00"）则原样解析即可。parseDbTime 统一两种情况：
// 无时区标记 → 按 UTC 解析；输出统一为补零的本地时间 "YYYY-MM-DD HH:mm:ss"。
export function parseDbTime(t) {
  if (!t) return null
  if (t instanceof Date) return t
  let s = String(t)
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(s)) {
    s = s.replace(' ', 'T') + 'Z'
  }
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

export function formatDateTime(t) {
  const d = parseDbTime(t)
  if (!d) return '-'
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}
