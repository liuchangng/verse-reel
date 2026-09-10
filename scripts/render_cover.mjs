// 用 @resvg/resvg-js 把 cover.svg 渲染成 1080x1080 PNG（加载系统字体，中文不出乱码）
// ESM import 不走 NODE_PATH，直接从绝对路径导入 resvg-js
import { Resvg } from 'file:///C:/Users/lcj/.workbuddy/binaries/node/workspace/node_modules/@resvg/resvg-js/index.js'
import { readFileSync, writeFileSync } from 'node:fs'

const svgPath = 'E:/workspace/python/cs-learning/gushicijieshuo/scripts/cover.svg'
const outPath = 'E:/workspace/python/cs-learning/gushicijieshuo/outputs/gushi_cover_1080.png'

const svg = readFileSync(svgPath, 'utf-8')
const resvg = new Resvg(svg, {
  fitTo: { mode: 'width', value: 1080 },
  background: 'rgba(0,0,0,0)',
  font: {
    loadSystemFonts: true,
    // 指定字体族兜底，确保中文走系统字体
    fontFamilies: ['Microsoft YaHei', 'SimHei'],
  },
})
const png = resvg.render()
const buf = png.asPng()
writeFileSync(outPath, buf)
console.log('OK', outPath, buf.length, 'bytes')
