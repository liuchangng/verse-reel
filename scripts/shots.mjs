// 访问 5173 逐页截图到 docs/images/（playwright-core + 系统 Chrome 作内核）
// 中文页面走系统字体，正常渲染。每页等网络空闲 + 数据加载完再截，避免空页。
import { chromium } from 'file:///C:/Users/lcj/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { writeFileSync } from 'node:fs'

const BASE = 'http://localhost:5173'
const OUT = 'E:/workspace/python/cs-learning/gushicijieshuo/docs/images/'
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const VIEW = { width: 1440, height: 900 }

// 要截的页面（顺序即 README 展示顺序）
const pages = [
  { key: 'hot',        url: `${BASE}/hot` },
  { key: 'tasks',      url: `${BASE}/tasks` },
  { key: 'task-detail',url: `${BASE}/tasks/5` },   // task 5 done，含进度/视频/文案
  { key: 'poetry',     url: `${BASE}/poetry` },
  { key: 'settings',   url: `${BASE}/settings` },
]

const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--force-color-profile=srgb'],
})

const results = []
try {
  const ctx = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await ctx.newPage()

  for (const p of pages) {
    try {
      await page.goto(p.url, { waitUntil: 'networkidle', timeout: 30000 })
      // 再留一拍让 SPA 数据渲染 / 图片加载
      await page.waitForTimeout(1500)
      const file = OUT + `shot_${p.key}.png`
      await page.screenshot({ path: file, fullPage: p.key === 'task-detail' })
      results.push(`OK  ${p.key} -> ${file}`)
    } catch (e) {
      results.push(`ERR ${p.key} @ ${p.url} :: ${e.message.split('\n')[0]}`)
    }
  }
} finally {
  await browser.close()
}

console.log(results.join('\n'))
writeFileSync(OUT + 'shots_report.txt', results.join('\n'))
console.log('\nDONE')
