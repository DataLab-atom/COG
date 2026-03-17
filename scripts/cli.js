#!/usr/bin/env node
/**
 * cli.js — `cog-mcts` CLI entry point
 * Usage:
 *   npx cog-mcts setup                     # configure all supported platforms
 *   npx cog-mcts setup --platform claude   # Claude Code only
 *   npx cog-mcts setup --platform cursor
 *   npx cog-mcts setup --platform windsurf
 *   npx cog-mcts setup --platform openclaw
 */

const { execSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const PKG_ROOT = path.resolve(__dirname, '..');
const SKILLS_DIR = path.join(PKG_ROOT, 'plugin', 'skills');
const AGENTS_MD = path.join(PKG_ROOT, 'plugin', 'AGENTS.md');
const AGENTS_DIR = path.join(PKG_ROOT, 'plugin', 'agents');

const MCP_SERVER_ENTRY = {
  command: 'mcts-engine',
  type: 'stdio',
  args: []
};

// ── helpers ───────────────────────────────────────────────────────────────────

function readJSON(filePath) {
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); }
  catch (_) { return {}; }
}

function writeJSON(filePath, obj) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

function merge(target, source) {
  for (const [k, v] of Object.entries(source)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      target[k] = merge(target[k] || {}, v);
    } else {
      target[k] = v;
    }
  }
  return target;
}

// ── platform setups ───────────────────────────────────────────────────────────

function setupClaude() {
  const settingsPath = path.join(os.homedir(), '.claude', 'settings.json');
  const settings = readJSON(settingsPath);
  merge(settings, { mcpServers: { 'mcts-engine': MCP_SERVER_ENTRY } });
  writeJSON(settingsPath, settings);
  console.log(`  Updated ${settingsPath}`);

  // Link skills into ~/.claude/skills/
  const claudeSkills = path.join(os.homedir(), '.claude', 'skills');
  fs.mkdirSync(claudeSkills, { recursive: true });
  for (const skill of fs.readdirSync(SKILLS_DIR)) {
    const src = path.join(SKILLS_DIR, skill);
    const dst = path.join(claudeSkills, skill);
    if (!fs.existsSync(dst)) {
      fs.symlinkSync(src, dst);
      console.log(`  Linked skill: ${skill}`);
    } else {
      console.log(`  Skill already exists: ${skill}`);
    }
  }
}

function setupCursor(projectDir) {
  const base = projectDir || process.cwd();
  const mcpPath = path.join(base, '.cursor', 'mcp.json');
  const mcp = readJSON(mcpPath);
  merge(mcp, { mcpServers: { 'mcts-engine': MCP_SERVER_ENTRY } });
  writeJSON(mcpPath, mcp);
  console.log(`  Updated ${mcpPath}`);

  // Copy AGENTS.md as Cursor rule
  const rulesDir = path.join(base, '.cursor', 'rules');
  fs.mkdirSync(rulesDir, { recursive: true });
  fs.copyFileSync(AGENTS_MD, path.join(rulesDir, 'cog-agents.md'));
  console.log(`  Copied AGENTS.md -> .cursor/rules/cog-agents.md`);

  if (fs.existsSync(AGENTS_DIR)) {
    for (const agentFile of fs.readdirSync(AGENTS_DIR)) {
      fs.copyFileSync(
        path.join(AGENTS_DIR, agentFile),
        path.join(rulesDir, `cog-${agentFile}`)
      );
      console.log(`  Copied agents/${agentFile} -> .cursor/rules/cog-${agentFile}`);
    }
  }
}

function setupWindsurf() {
  const mcpPath = path.join(os.homedir(), '.codeium', 'windsurf', 'mcp_config.json');
  const mcp = readJSON(mcpPath);
  merge(mcp, { mcpServers: { 'mcts-engine': MCP_SERVER_ENTRY } });
  writeJSON(mcpPath, mcp);
  console.log(`  Updated ${mcpPath}`);
}

function setupOpenclaw() {
  const extDir = path.join(os.homedir(), '.openclaw', 'extensions', 'openclaw-mcts');
  const pluginSrc = path.join(PKG_ROOT, 'plugin');

  fs.mkdirSync(extDir, { recursive: true });
  spawnSync('cp', ['-r', `${pluginSrc}/.`, extDir], { stdio: 'inherit' });
  console.log(`  Plugin copied to ${extDir}`);

  const configPath = path.join(os.homedir(), '.openclaw', 'openclaw.json');
  const config = readJSON(configPath);
  merge(config, {
    plugins: { entries: { 'openclaw-mcts': { enabled: true, config: {} } } },
    mcpServers: { 'mcts-engine': { command: 'mcts-engine', args: [], env: {} } }
  });
  writeJSON(configPath, config);
  console.log(`  Updated ${configPath}`);
  console.log('  Run: openclaw gateway restart');
}

// ── main ──────────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);
const cmd = args[0];
const platformIdx = args.indexOf('--platform');
const platform = platformIdx !== -1 ? args[platformIdx + 1] : 'all';

if (cmd !== 'setup') {
  console.log('Usage:');
  console.log('  npx cog-mcts setup [--platform claude|cursor|windsurf|openclaw|all]');
  process.exit(0);
}

console.log(`\nCOG MCTS setup (platform: ${platform})\n`);

const all = platform === 'all';

if (all || platform === 'claude') {
  console.log('Claude Code:');
  try { setupClaude(); } catch (e) { console.error('  ERROR:', e.message); }
}
if (all || platform === 'cursor') {
  console.log('Cursor:');
  try { setupCursor(); } catch (e) { console.error('  ERROR:', e.message); }
}
if (all || platform === 'windsurf') {
  console.log('Windsurf:');
  try { setupWindsurf(); } catch (e) { console.error('  ERROR:', e.message); }
}
if (all || platform === 'openclaw') {
  console.log('OpenClaw:');
  try { setupOpenclaw(); } catch (e) { console.error('  ERROR:', e.message); }
}

console.log('\nDone! Type /mcts-status in chat to verify installation.\n');
