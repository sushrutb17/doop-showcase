import { spawnSync } from "node:child_process";

function configuredEnvironment(server) {
  const configured = server?.env;
  if (!configured || typeof configured !== "object" || Array.isArray(configured)) {
    return process.env;
  }
  const strings = Object.fromEntries(
    Object.entries(configured).filter(([, value]) => typeof value === "string"),
  );
  return { ...process.env, ...strings };
}

export function isMainDoopReady(config, run = spawnSync) {
  const server = config?.mcp?.servers?.doop;
  if (!server || server.enabled === false || typeof server.command !== "string") {
    return false;
  }
  const args = Array.isArray(server.args) ? server.args : [];
  if (!args.every((arg) => typeof arg === "string")) {
    return false;
  }
  const result = run(
    server.command,
    [...args, "--healthcheck"],
    {
      cwd: typeof server.cwd === "string" ? server.cwd : undefined,
      env: configuredEnvironment(server),
      encoding: "utf8",
      stdio: "ignore",
      timeout: 2000,
      windowsHide: true,
    },
  );
  return !result.error && result.status === 0 && !result.signal;
}
