// 把 ascii-sharks 的 5 个 TS 源文件转成一个可在浏览器里直接用的 IIFE bundle。
// 用 Node 24 内置的 stripTypeScriptTypes(transform) 剥离类型，零外部依赖。
const { stripTypeScriptTypes } = require('node:module');
const fs = require('node:fs');
const path = require('node:path');

const DIR = path.join(__dirname, 'ascii-sharks');
const ORDER = ['math', 'species', 'world', 'renderer', 'engine'];
const parts = [];
const exported = new Set();

for (const name of ORDER) {
  const src = fs.readFileSync(path.join(DIR, name + '.ts'), 'utf8');
  let js = stripTypeScriptTypes(src, { mode: 'transform' });

  // 记下所有具名导出（供末尾统一挂 window 用）
  for (const m of js.matchAll(/export (?:class|const|function|let|var)\s+([A-Za-z_$][\w$]*)/g)) {
    exported.add(m[1]);
  }
  // 去掉 import 语句（我们按顺序内联，同一个作用域里直接可见）
  js = js.replace(/^\s*import[\s\S]*?from\s+["'][^"']+["'];\s*$/gm, '');
  js = js.replace(/^\s*import\s+["'][^"']+["'];\s*$/gm, '');
  // 去掉 export 关键字（含 export {..} / export type {..}）
  js = js.replace(/^\s*export\s+type\s*\{[^}]*\};?\s*$/gm, '');
  js = js.replace(/^\s*export\s*\{[^}]*\};?\s*$/gm, '');
  js = js.replace(/^\s*export\s+/gm, '');

  parts.push(`/* ==== ${name}.ts ==== */\n${js.trim()}\n`);
}

const banner = `/*!
 * ascii-sharks — https://github.com/stackedlol/ascii-sharks (MIT)
 * 原仓库是 TypeScript 组件；这里用 Node 内置的 stripTypeScriptTypes 转成纯 JS，
 * 内联进控制台页面，避免额外构建与网络请求。只保留了画布引擎，
 * React 封装 (react.tsx) 未包含。
 */
`;
const expose = `
// 暴露给页面脚本
window.AsciiSharks = { ${[...exported].join(', ')} };
`;

const out = banner + '(function () {\n"use strict";\n' + parts.join('\n') + expose + '})();\n';
fs.writeFileSync(path.join(__dirname, 'ascii-sharks.bundle.js'), out);
console.log('bundle bytes:', out.length, '| exports:', [...exported].join(', '));
