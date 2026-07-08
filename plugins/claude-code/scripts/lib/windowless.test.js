/**
 * Tests for the pythonw-detection helper in memory.js. Run with:
 *   node --test plugins/claude-code/scripts/lib/windowless.test.js
 *
 * REGRESSION GUARD / IMPLEMENTATION GAP — intentionally RED against the
 * current tree. memory.js defines `_windowlessPythonw` (and
 * `spawnDetached`) as private, unexported helpers — the file's only export
 * is `module.exports = { createMemoryClient }` (see memory.js:289). These
 * tests target the INTENDED post-fix API where memory.js additionally
 * exports `_windowlessPythonw` so its pythonw-resolution logic can be unit
 * tested in isolation from `spawnDetached`/`createMemoryClient`. Until the
 * coder adds that export, every test below fails immediately with
 * "_windowlessPythonw is not a function" — RED because of the missing
 * export, not because the underlying logic (already reviewed) is wrong.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { _windowlessPythonw } = require("./memory");

function withPlatform(value, fn) {
  const original = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", original);
  }
}

function withEnv(overrides, fn) {
  const previous = {};
  for (const key of Object.keys(overrides)) {
    previous[key] = process.env[key];
    if (overrides[key] === undefined) delete process.env[key];
    else process.env[key] = overrides[key];
  }
  try {
    return fn();
  } finally {
    for (const key of Object.keys(previous)) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  }
}

test("returns null off win32", () => {
  withPlatform("darwin", () => {
    assert.equal(_windowlessPythonw(), null);
  });
});

test("returns CODE_MEMORY_PYTHONW override when set + exists", (t) => {
  const overridePath = "/fake/override/pythonw.exe";
  t.mock.method(fs, "existsSync", (p) => p === overridePath);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: overridePath, APPDATA: undefined }, () => {
      assert.equal(_windowlessPythonw(), overridePath);
    });
  });
});

test("ignores CODE_MEMORY_PYTHONW override when it does not exist", (t) => {
  const overridePath = "/fake/missing/pythonw.exe";
  t.mock.method(fs, "existsSync", () => false);
  t.mock.method(fs, "readdirSync", () => []);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: overridePath, APPDATA: "/fake/appdata" }, () => {
      assert.equal(_windowlessPythonw(), null);
    });
  });
});

test("scans APPDATA/uv/tools for code-memory", (t) => {
  const appdata = "/fake/appdata";
  const toolsDir = path.join(appdata, "uv", "tools");
  const pywPath = path.join(toolsDir, "code-memory", "Scripts", "pythonw.exe");

  t.mock.method(fs, "readdirSync", (dir) => (dir === toolsDir ? ["code-memory"] : []));
  t.mock.method(fs, "existsSync", (p) => p === pywPath);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: undefined, APPDATA: appdata }, () => {
      assert.equal(_windowlessPythonw(), pywPath);
    });
  });
});

test("scans APPDATA/uv/tools case-insensitively and skips non-matching dirs", (t) => {
  const appdata = "/fake/appdata";
  const toolsDir = path.join(appdata, "uv", "tools");
  const pywPath = path.join(toolsDir, "CODE-MEMORY-cli", "Scripts", "pythonw.exe");

  t.mock.method(fs, "readdirSync", (dir) =>
    dir === toolsDir ? ["some-other-tool", "CODE-MEMORY-cli"] : [],
  );
  t.mock.method(fs, "existsSync", (p) => p === pywPath);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: undefined, APPDATA: appdata }, () => {
      assert.equal(_windowlessPythonw(), pywPath);
    });
  });
});

test("returns null when nothing found", (t) => {
  t.mock.method(fs, "readdirSync", () => []);
  t.mock.method(fs, "existsSync", () => false);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: undefined, APPDATA: "/fake/appdata" }, () => {
      assert.equal(_windowlessPythonw(), null);
    });
  });
});

test("returns null when APPDATA is unset and no override given", (t) => {
  t.mock.method(fs, "existsSync", () => false);

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: undefined, APPDATA: undefined }, () => {
      assert.equal(_windowlessPythonw(), null);
    });
  });
});

test("returns null when readdirSync throws (tools dir missing)", (t) => {
  t.mock.method(fs, "existsSync", () => false);
  t.mock.method(fs, "readdirSync", () => {
    throw new Error("ENOENT: no such directory");
  });

  withPlatform("win32", () => {
    withEnv({ CODE_MEMORY_PYTHONW: undefined, APPDATA: "/fake/appdata" }, () => {
      assert.equal(_windowlessPythonw(), null);
    });
  });
});
