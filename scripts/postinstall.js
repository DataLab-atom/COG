#!/usr/bin/env node
/**
 * postinstall.js — 自动安装 Python MCP server（mcts-engine）
 * 在 `npm install cog-mcts` 完成后自动触发。
 */

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const PKG_ROOT = path.resolve(__dirname, '..');
const MCTS_ENGINE_DIR = path.join(PKG_ROOT, 'plugin', 'mcts-engine');

function run(cmd, opts = {}) {
  console.log(`  $ ${cmd}`);
  execSync(cmd, { stdio: 'inherit', ...opts });
}

function checkPython() {
  for (const bin of ['python3', 'python']) {
    try {
      const ver = execSync(`${bin} --version 2>&1`).toString().trim();
      const match = ver.match(/(\d+)\.(\d+)/);
      if (match && (parseInt(match[1]) > 3 || (parseInt(match[1]) === 3 && parseInt(match[2]) >= 11))) {
        return bin;
      }
    } catch (_) {}
  }
  return null;
}

console.log('\nCOG MCTS: Installing Python MCP server...\n');

const python = checkPython();
if (!python) {
  console.warn('  Python >= 3.11 not found. Please install it manually:');
  console.warn(`   pip install ${MCTS_ENGINE_DIR}`);
  console.warn('   Then re-run: npm run postinstall\n');
  process.exit(0);
}

try {
  run(`${python} -m pip install --quiet "${MCTS_ENGINE_DIR}"`);
  console.log('\n  mcts-engine installed successfully.\n');
  console.log('Next steps:');
  console.log('  npx cog-mcts setup                   — configure Claude Code / Cursor / Windsurf');
  console.log('  npx cog-mcts setup --platform claude — Claude Code only\n');
} catch (err) {
  console.error('\n  pip install failed:', err.message);
  console.error(`   Please run manually: pip install "${MCTS_ENGINE_DIR}"\n`);
  process.exit(0);
}
