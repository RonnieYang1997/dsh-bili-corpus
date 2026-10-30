#!/bin/bash
# dsh-bili-corpus 构建：自包含（不依赖 DSH checkout、不联网）。
#
#   host  → lib/index.js   （tsc，NodeNext，ESM）
#   client→ lib/client.js  （tsdown，CJS + window.__ModuleLoader__ 外壳）
#
# 为什么不用 dev_scaffold_plugin 的模板：它要求 DSH_CHECKOUT 指到 dsh 源码仓，
# 本机没有源码（只有 app.asar），所以模板里的 junction 链接与 CHECKOUT tsc 都不可用。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NODE_BIN="${NODE_BIN:-node}"

echo "=== tsc → lib/ (host) ==="
"$NODE_BIN" node_modules/typescript/bin/tsc -p tsconfig.json

echo "=== tsdown → lib/client.js (client) ==="
"$NODE_BIN" node_modules/tsdown/dist/run.mjs

echo "=== verify outputs ==="
test -f lib/index.js || { echo "build: lib/index.js missing" >&2; exit 1; }
test -f lib/client.js || { echo "build: lib/client.js missing" >&2; exit 1; }
node -e "
const fs = require('fs');
const c = fs.readFileSync('lib/client.js', 'utf8');
if (!c.includes('__ModuleLoader__.load')) throw new Error('client bundle missing ModuleLoader banner');
if (!c.includes('register(')) throw new Error('client bundle missing register call');
console.log('ok  lib/index.js', fs.statSync('lib/index.js').size, 'B · lib/client.js', c.length, 'B');
"

echo "=== Build complete ==="
