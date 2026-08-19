import { build } from 'esbuild'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'

const ID = 'dsh-itdata'

// Seed words and shell-own modules the browser module table provides.
const externals = [
  'react',
  'react/jsx-runtime',
  'react-dom',
  'react-dom/client',
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-web-react',
  '@deepseek-ai/dsh-client-ui-primitives',
  '@deepseek-ai/dsh-client-ui-slots',
]

const result = await build({
  entryPoints: ['src/client.jsx'],
  bundle: true,
  format: 'cjs',
  platform: 'browser',
  jsx: 'automatic',
  jsxImportSource: 'react',
  minify: false,
  write: false,
  external: externals,
  target: ['es2022'],
  logLevel: 'info',
})

const code = result.outputFiles[0].text
const wrapped =
  `window.__ModuleLoader__.load({\n` +
  `\tid: ${JSON.stringify(ID)},\n` +
  `\tfactory: (require) => {\n` +
  `\t\tvar module = { exports: {} };\n` +
  `\t\tvar exports = module.exports;\n` +
  `\t\tObject.defineProperty(exports, Symbol.toStringTag, { value: "Module" });\n` +
  code +
  `\t\treturn module.exports;\n` +
  `\t}\n` +
  `});\n`

mkdirSync('lib', { recursive: true })
writeFileSync('lib/client.js', wrapped)

// Host half: plain ESM, no bundling needed.
writeFileSync('lib/index.js', readFileSync('src/host.js'))

console.log(`built lib/client.js (${(wrapped.length / 1024).toFixed(1)} KiB) and lib/index.js`)
