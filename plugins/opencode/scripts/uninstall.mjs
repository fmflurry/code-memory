#!/usr/bin/env node
//
// Remove the code-memory OpenCode plugin from the user's plugin directory
// and drop the MCP block from opencode.jsonc.

import { existsSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

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
    console.log("Usage: code-memory-opencode-uninstall [--project | --target DIR] [--no-mcp]");
    process.exit(0);
  } else {
    console.error(`Unknown flag: ${a}`);
    process.exit(2);
  }
}

if (mode === "global") target = join(homedir(), ".config", "opencode", "plugins");
else if (mode === "project") target = join(process.cwd(), ".opencode", "plugins");

for (const f of ["code-memory.ts", "code-memory-lib"]) {
  const p = join(target, f);
  if (existsSync(p)) {
    rmSync(p, { recursive: true, force: true });
    console.log(`removed ${p}`);
  }
}

if (skipMcp) process.exit(0);

const configPath = mode === "project"
  ? join(process.cwd(), "opencode.jsonc")
  : join(homedir(), ".config", "opencode", "opencode.jsonc");

if (!existsSync(configPath)) process.exit(0);

function stripJsonc(text) {
  text = text.replace(/\/\*[\s\S]*?\*\//g, "");
  text = text.replace(/(^|[^:"'])\/\/[^\n]*/g, "$1");
  text = text.replace(/,(\s*[}\]])/g, "$1");
  return text;
}

const raw = readFileSync(configPath, "utf8");
let data;
try { data = JSON.parse(stripJsonc(raw)); }
catch { console.error(`✗ could not parse ${configPath}; remove MCP block manually.`); process.exit(1); }

if (data?.mcp?.["code-memory"]) {
  writeFileSync(configPath + ".bak", raw);
  delete data.mcp["code-memory"];
  writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
  console.log(`removed MCP block from ${configPath} (backup at ${configPath}.bak)`);
}
