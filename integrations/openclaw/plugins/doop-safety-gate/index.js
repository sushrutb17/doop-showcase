import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { isMainDoopReady } from "./health-check.js";
import { safetyGateDecision } from "./safety-gate.js";

export default definePluginEntry({
  id: "doop-safety-gate",
  name: "Doop Safety Gate",
  description: "Stops safety-critical training turns before model inference.",
  register(api) {
    api.on(
      "before_agent_run",
      (event, ctx) =>
        safetyGateDecision(event.prompt, ctx.agentId, () => isMainDoopReady(api.config)),
      { priority: 1000, timeoutMs: 3000 },
    );
  },
});
