import assert from "node:assert/strict";
import test from "node:test";

import {
  OFFLINE_TEXT,
  SAFETY_TEXT,
  classifySafetySituation,
  safetyGateDecision,
} from "../safety-gate.js";

test("urgent symptoms win and block before inference", () => {
  assert.equal(
    classifySafetySituation("I feel dizzy and have sharp knee pain. Can I finish the set?"),
    "dizziness_or_chest_pain",
  );
  assert.deepEqual(
    safetyGateDecision("I have chest pain during this workout.", "doop"),
    {
      outcome: "block",
      reason: "doop_safety_gate:dizziness_or_chest_pain",
      message: SAFETY_TEXT.dizziness_or_chest_pain,
      category: "dizziness_or_chest_pain",
    },
  );
});

test("pain and illness receive deterministic situation-specific blocks", () => {
  assert.equal(classifySafetySituation("Sharp knee pain; should I push through?"), "pain_or_injury");
  assert.equal(
    classifySafetySituation("I have unusual severe fatigue and feel sick."),
    "illness_or_severe_fatigue",
  );
});

test("negated symptoms do not trigger", () => {
  assert.equal(classifySafetySituation("No chest pain and not dizzy; what is today's plan?"), null);
  assert.equal(classifySafetySituation("I am not sick and have no sharp knee pain."), null);
  assert.equal(classifySafetySituation("I don't feel dizzy and have no severe knee pain."), null);
  assert.equal(classifySafetySituation("My knee does not hurt more with each rep."), null);
});

test("lack of relief or improvement does not negate an affirmed pain report", () => {
  assert.equal(
    classifySafetySituation("I have no relief from sharp knee pain."),
    "pain_or_injury",
  );
  assert.equal(
    classifySafetySituation("There is no improvement and I have severe knee pain."),
    "pain_or_injury",
  );
});

test("worsening hurt phrasing fails closed", () => {
  assert.equal(classifySafetySituation("My knee hurts more every rep."), "pain_or_injury");
  assert.equal(classifySafetySituation("My shoulder is hurting worse today."), "pain_or_injury");
});

test("normal training requests and other agents pass", () => {
  assert.deepEqual(safetyGateDecision("What's my training plan today?", "doop"), { outcome: "pass" });
  assert.deepEqual(safetyGateDecision("I have chest pain.", "main"), { outcome: "pass" });
});

test("disabled main Doop server fails closed before model inference", () => {
  assert.deepEqual(safetyGateDecision("Is my recovery good enough?", "doop", false), {
    outcome: "block",
    reason: "doop_safety_gate:training_data_offline",
    message: OFFLINE_TEXT,
    category: "training_data_offline",
  });
  assert.deepEqual(safetyGateDecision("Is my recovery good enough?", "main", false), {
    outcome: "pass",
  });
});

test("urgent safety guidance takes precedence while training data is offline", () => {
  assert.equal(
    safetyGateDecision("I have chest pain during this set.", "doop", false).category,
    "dizziness_or_chest_pain",
  );
});
