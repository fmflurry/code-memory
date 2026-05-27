#!/usr/bin/env node
//
// Install the `code-memory-opencode` plugin into the user's OpenCode plugin
// directory and register the code-memory MCP server in opencode.jsonc.
//
// Copies the plugin entry + lib into:
//   --project        $PWD/.opencode/plugins/
//   --target DIR     <DIR>
//   (default)        ~/.config/opencode/plugins/
//
// Idempotent. User rejected symlink installs — we always copy.

import { existsSync, mkdirSync, copyFileSync, rmSync, cpSync, readFileSync, writeFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(HERE, "..");
const ENTRY = join(PKG_ROOT, "src", "code-memory.ts");
const LIB = join(PKG_ROOT, "src", "code-memory-lib");

const args = process.argv.slice(2);
let mode = "global";
let target = "";
let skipMcp = false;

for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--project") mode = "project";
  else if (a === "--target") { mode = "custom"; target = args[++i]; }
  else if (a === "--no-mcp") skipMcp = true;
  else if (a === "-h" || a === "--help") {
    console.log(`Usage: code-memory-opencode-install [--project | --target DIR] [--no-mcp]

  (default)     install globally at ~/.config/opencode/plugins/
  --project     install at \$PWD/.opencode/plugins/
  --target DIR  install into DIR
  --no-mcp      skip registering the code-memory MCP server`);
    process.exit(0);
  } else {
    console.error(`Unknown flag: ${a}`);
    process.exit(2);
  }
}

if (mode === "global") target = join(homedir(), ".config", "opencode", "plugins");
else if (mode === "project") target = join(process.cwd(), ".opencode", "plugins");

if (!existsSync(ENTRY)) {
  console.error(`Missing ${ENTRY} — package layout broken.`);
  process.exit(1);
}
if (!existsSync(LIB)) {
  console.error(`Missing ${LIB} — package layout broken.`);
  process.exit(1);
}

mkdirSync(target, { recursive: true });
console.log(`Installing plugin into: ${target}`);

const entryDst = join(target, "code-memory.ts");
const libDst = join(target, "code-memory-lib");

if (existsSync(entryDst)) rmSync(entryDst, { force: true });
copyFileSync(ENTRY, entryDst);
console.log(`  ${entryDst}`);

if (existsSync(libDst)) rmSync(libDst, { recursive: true, force: true });
cpSync(LIB, libDst, { recursive: true });
console.log(`  ${libDst}/`);

// ---------- MCP block ----------
if (skipMcp) {
  console.log("\nSkipping MCP registration (--no-mcp).");
  process.exit(0);
}

const REPO_URL = process.env.CODEMEMORY_REPO_URL || "https://github.com/fmflurry/code-memory";
const MCP_BLOCK = {
  type: "local",
  command: ["uvx", "--from", `git+${REPO_URL}`, "code-memory-mcp"],
  enabled: true,
  environment: { CODE_MEMORY_PROJECT: "auto" },
};

const configPath = mode === "project"
  ? join(process.cwd(), "opencode.jsonc")
  : join(homedir(), ".config", "opencode", "opencode.jsonc");

function stripJsonc(text) {
  text = text.replace(/\/\*[\s\S]*?\*\//g, "");
  text = text.replace(/(^|[^:"'])\/\/[^\n]*/g, "$1");
  text = text.replace(/,(\s*[}\]])/g, "$1");
  return text;
}

let data = {};
let hadFile = false;
if (existsSync(configPath)) {
  hadFile = true;
  const raw = readFileSync(configPath, "utf8");
  try {
    data = JSON.parse(stripJsonc(raw));
  } catch (e) {
    console.error(`✗ failed to parse ${configPath}: ${e.message}`);
    console.error("  add the MCP block manually (see README §MCP server).");
    process.exit(1);
  }
  writeFileSync(configPath + ".bak", raw);
  console.error(`  backup: ${configPath}.bak`);
} else {
  mkdirSync(dirname(configPath), { recursive: true });
}

if (typeof data !== "object" || Array.isArray(data) || data === null) {
  console.error(`✗ ${configPath} is not a JSON object; cannot merge.`);
  process.exit(1);
}

if (!data.$schema) data.$schema = "https://opencode.ai/config.json";
if (!data.mcp || typeof data.mcp !== "object" || Array.isArray(data.mcp)) data.mcp = {};

if (data.mcp["code-memory"]) {
  console.log(`✓ code-memory MCP already configured in ${configPath}`);
} else {
  data.mcp["code-memory"] = MCP_BLOCK;
  writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
  console.log(`✓ wrote MCP block to ${configPath}`);
  if (configPath.endsWith(".jsonc") && hadFile) {
    console.error("  note: JSONC comments were not preserved; original is in the .bak file.");
  }
}

console.log("\nRestart OpenCode to pick up the new plugin and MCP server.");
if (mode === "project") {
  console.log(`(commit ${configPath} so teammates get it too)`);
}
