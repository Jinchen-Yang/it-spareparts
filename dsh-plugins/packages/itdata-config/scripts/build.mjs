import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'

const ID = 'dsh-itdata-config'

mkdirSync('lib', { recursive: true })
writeFileSync('lib/index.js', readFileSync('src/host.js'))
console.log(`built lib/index.js (host-only, no client bundle for ${ID})`)
