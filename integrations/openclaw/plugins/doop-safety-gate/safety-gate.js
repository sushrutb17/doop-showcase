import { readFileSync } from "node:fs";

const MESSAGES = JSON.parse(
  readFileSync(new URL("./safety-messages.json", import.meta.url), "utf8"),
);
export const OFFLINE_TEXT = MESSAGES.offline;
export const SAFETY_TEXT = Object.freeze(MESSAGES.safety);

// Suppress a safety match only when the negation is attached directly to the
// symptom. A broad look-behind is unsafe here: "no relief from sharp pain" and
// "no improvement and I have severe pain" both affirm a worsening problem.
const DIRECT_NEGATION_PREFIX =
  /(?:\b(?:no|without)\s+(?:any\s+)?|\b(?:not|never)\s+(?:(?:feeling|experiencing|having)\s+)?|\b(?:do(?:es)?|did)\s+not\s+(?:(?:feel|have|experience)\s+)?|\b(?:don['’]?t|doesn['’]?t|didn['’]?t)\s+(?:(?:feel|have|experience)\s+)?|\b(?:am|are|is|was|were)\s+not\s+(?:(?:feeling|experiencing|having)\s+)?|\b(?:aren['’]?t|isn['’]?t|wasn['’]?t|weren['’]?t)\s+(?:(?:feeling|experiencing|having)\s+)?)$/i;

const SITUATION_PATTERNS = Object.freeze([
  [
    "dizziness_or_chest_pain",
    [
      /\bchest\s+(?:pain|pressure|tightness|discomfort)\b/gi,
      /\b(?:dizz(?:y|iness)|light[- ]?headed(?:ness)?|faint(?:ed|ing)?|pass(?:ed|ing)?\s+out)\b/gi,
    ],
  ],
  [
    "pain_or_injury",
    [
      /\b(?:sharp|severe|worsening|sudden)\s+(?:[a-z]+\s+){0,2}pain\b/gi,
      /\b(?:hurt(?:s|ing)?|ache(?:s|ing)?)\s+(?:more|worse)\b/gi,
      /\b(?:injur(?:y|ed)|numb(?:ness)?|deform(?:ity|ed)|(?:loss|lack)\s+of\s+(?:strength|sensation))\b/gi,
    ],
  ],
  [
    "illness_or_severe_fatigue",
    [
      /\b(?:ill|sick|fever(?:ish)?|vomit(?:ing)?|flu)\b/gi,
      /\b(?:severe|unusual|extreme)\s+fatigue\b/gi,
    ],
  ],
]);

function hasNonNegatedMatch(text, pattern) {
  pattern.lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    const prefix = text.slice(Math.max(0, match.index - 48), match.index);
    if (!DIRECT_NEGATION_PREFIX.test(prefix)) {
      return true;
    }
  }
  return false;
}

export function classifySafetySituation(prompt) {
  if (typeof prompt !== "string" || !prompt.trim()) {
    return null;
  }
  for (const [situation, patterns] of SITUATION_PATTERNS) {
    if (patterns.some((pattern) => hasNonNegatedMatch(prompt, pattern))) {
      return situation;
    }
  }
  return null;
}

export function safetyGateDecision(prompt, agentId, mainDoopReady = true) {
  if (agentId !== "doop") {
    return { outcome: "pass" };
  }
  const situation = classifySafetySituation(prompt);
  if (!situation) {
    const ready = typeof mainDoopReady === "function" ? mainDoopReady() : mainDoopReady;
    if (ready) {
      return { outcome: "pass" };
    }
    return {
      outcome: "block",
      reason: "doop_safety_gate:training_data_offline",
      message: OFFLINE_TEXT,
      category: "training_data_offline",
    };
  }
  return {
    outcome: "block",
    reason: `doop_safety_gate:${situation}`,
    message: SAFETY_TEXT[situation],
    category: situation,
  };
}
