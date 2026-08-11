import assert from "node:assert/strict";
import test from "node:test";

import { isMainDoopReady } from "../health-check.js";

function config(overrides = {}) {
  return {
    mcp: {
      servers: {
        doop: {
          enabled: true,
          command: "/venv/bin/python",
          args: ["-m", "whoop_local.mcp_server.server", "--db", "/data/doop.db"],
          cwd: "/opt/doop",
          ...overrides,
        },
      },
    },
  };
}

test("readiness probe uses the exact configured command shape without a shell", () => {
  let observed;
  const ready = isMainDoopReady(config(), (command, args, options) => {
    observed = { command, args, options };
    return { status: 0, signal: null };
  });
  assert.equal(ready, true);
  assert.equal(observed.command, "/venv/bin/python");
  assert.deepEqual(observed.args, [
    "-m",
    "whoop_local.mcp_server.server",
    "--db",
    "/data/doop.db",
    "--healthcheck",
  ]);
  assert.equal(observed.options.cwd, "/opt/doop");
  assert.equal(observed.options.timeout, 2000);
  assert.equal("shell" in observed.options, false);
});

test("missing, disabled, failed, and timed-out primary servers fail closed", () => {
  assert.equal(isMainDoopReady({}, () => ({ status: 0 })), false);
  assert.equal(isMainDoopReady(config({ enabled: false }), () => ({ status: 0 })), false);
  assert.equal(isMainDoopReady(config(), () => ({ status: 1, signal: null })), false);
  assert.equal(
    isMainDoopReady(config(), () => ({ status: null, signal: "SIGTERM", error: new Error() })),
    false,
  );
});

test("documented uv run command and configured environment are preserved", () => {
  let observed;
  const ready = isMainDoopReady(
    config({
      command: "uv",
      args: ["run", "doop-mcp", "--db", "/data/isolated.db"],
      env: { DOOP_TIMEZONE: "Etc/UTC", IGNORED_NON_STRING: 17 },
    }),
    (command, args, options) => {
      observed = { command, args, options };
      return { status: 0, signal: null };
    },
  );
  assert.equal(ready, true);
  assert.equal(observed.command, "uv");
  assert.deepEqual(observed.args, [
    "run",
    "doop-mcp",
    "--db",
    "/data/isolated.db",
    "--healthcheck",
  ]);
  assert.equal(observed.options.env.DOOP_TIMEZONE, "Etc/UTC");
  assert.equal("IGNORED_NON_STRING" in observed.options.env, false);
});

test("non-string configured arguments fail closed", () => {
  assert.equal(isMainDoopReady(config({ args: ["run", 17] }), () => ({ status: 0 })), false);
});
