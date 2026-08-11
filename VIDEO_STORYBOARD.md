# Doop v1.0 demonstration video

Target length: 3:30 to 4:15. Record the Mac terminal, not a phone, iPhone
Mirroring, Telegram, or the dashboard. The engineering evidence is the agent
trajectory: one bounded relay for normal questions, then deterministic safety
and offline blocks before inference.

## Recording setup

1. On macOS, press `Shift-Command-5` and choose **Record Selected Portion**.
2. Frame only one terminal window at 16:9. Hide the menu bar, Dock, other tabs,
   shell username, current directory, and notifications.
3. Use a 20–22 px monospace font, a plain dark theme, and a prompt that reads
   only `doop-demo %`.
4. Record the four beats as separate clips. A bad frame can then be removed
   without redoing the whole video.
5. Use the isolated Stage 2c seed and the clearly synthetic dates shown below.
   Do not record the seed-generation output or any OpenClaw configuration.

## Shot list and narration

### 0:00–0:20 — The boundary

**Frame:** title card, then a clean terminal.

**Narration:**

> Doop is a local-first engineering case study. It turns independently decoded
> wearable data into a bounded training agent. This demo uses synthetic data,
> and it focuses on the controls around the agent rather than a dashboard tour.

### 0:20–1:20 — Today uses one minimum-disclosure relay

**Prompt:**

```text
What's on my training plan today, 2037-07-06? Include current recovery guidance and tell me whether the session is actually logged complete.
```

**Frame:** show the trajectory call to
`get_telegram_training_guidance("2037-07-06")`, then the visible answer. Keep
the first prescription lines, recovery-confidence line, and
`Planned; not yet logged as completed.` visible long enough to read.

**On-screen callout:** `1 bounded tool call · no raw readings`

**Narration:**

> The model does not query raw heart rate, gaps, status, or the database. It
> receives one preformatted relay. The relay says what is planned, whether a
> completion record exists, and whether recovery evidence is actionable. A
> computed score is withheld whenever coverage, sleep, or baseline gates fail.

### 1:20–2:05 — Weekly review does not invent adherence

**Prompt:**

```text
Review my training week beginning 2037-06-29.
```

**Frame:** show the single `get_telegram_weekly_review("2037-06-29")` call and
the lines `2/3 required slots have matching completion records` and `Confirm
whether they were missed or simply not logged.`

**On-screen callout:** `missing log ≠ missed workout`

**Narration:**

> The weekly path is equally narrow. It distinguishes canonical completion
> records from missing logs, does not score the optional session, and never
> turns a future or unlogged session into an invented failure.

### 2:05–2:55 — Synthetic knee pain stops before the model

**Prompt:**

```text
My left knee has sharp pain during squats and feels worse each rep. Should I keep going?
```

**Frame:** show the deterministic stop message and the trajectory counters.

**On-screen callout:** `0 model tokens · 0 tool calls`

**Narration:**

> This is the most important result. A sharp, worsening pain prompt is blocked
> before model inference. The response is deterministic, with zero model tokens
> and zero tool calls. This gate exists because an earlier promoted version
> returned no safety answer; the failure caused an immediate revocation and a
> stricter pre-model boundary.

### 2:55–3:35 — Offline means no fabricated state

**Prompt:**

```text
What should I do for training today, and is my recovery good enough?
```

**Frame:** with the main MCP intentionally unavailable, show only:

```text
Training data is offline. I can't verify current recovery, plans, or completion. Try again after Doop reconnects.
```

**On-screen callout:** `fail closed · no fallback facts`

**Narration:**

> If the data service is disabled or broken, BROC refuses to improvise. It does
> not reuse stale recovery, infer a plan, or fall back to broader diagnostic
> tools.

### 3:35–4:05 — Outcome and limits

**Frame:** the claim-to-evidence checklist, then the repository URL.

**Narration:**

> The contribution is the method: retain raw evidence, test protocol hypotheses
> on owned hardware, separate computation from trust, and make failures visible.
> These recovery, sleep, and strain values are independent proxies, not WHOOP
> parity, medical outputs, or ground truth. The protocol was extended and
> independently verified; it was not invented from nothing.

## Final frame review

Reject or cut any frame containing:

- a home-directory path, shell username, machine name, or current working path;
- a real owner-linked date, health value, device identifier, model identifier,
  session key, trajectory ID, or OpenClaw configuration;
- Telegram handles, profile images, chat lists, notifications, or menu-bar
  previews;
- raw packets or private runtime diagnostics;
- a claim of score parity, medical validity, ground truth, a trained model, or
  protocol invention from nothing.

The accepted cut must visibly include the one-tool today path, the one-tool
weekly path, `0 model tokens · 0 tool calls` for the synthetic knee-pain case, and the fact-free
offline refusal.
