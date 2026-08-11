# MCP_SPEC.md

> The contract for the local MCP server. Implemented with the Python MCP SDK /
> FastMCP over **stdio** (local; no auth). Each tool returns JSON-serializable
> data.

> **Synthetic examples:** every date, timestamp, identifier, device reading,
> workout, and physiological value in this document is invented and placed in
> a future 2042 epoch. None describes the repository owner or a real capture.

## Transport & wiring

- **Transport:** stdio. The agent (Claude Code / Desktop / Codex) launches it.
- **No auth.** Local-only, single user.
- **Read-only, fail closed.** The database must already exist. The server opens
  it with SQLite URI `mode=ro`, enables `PRAGMA query_only=ON`, and never writes
  or triggers captures. Capture and database initialization are separate local
  processes.
- **Current database contract:** schema v14.

### Optional agent consumer boundary

An optional local agent can launch this server as a stdio MCP child and filter
it to the required read tools; the MCP server is **not** exposed over the
network. No general MCP write tool is added, and agent payloads must never
contain raw databases, exports, routes, or sensor streams. Registration is a
user-local client action, not repository wiring; see `USING_THIS_CODE.md`.

## Conventions

- Dates `YYYY-MM-DD` (local day). Timestamps in returns are ISO-8601 UTC.
- Every metric return includes a `note` reminding these are independent proxies, not WHOOP scores.
- Missing data → explicit `{ "status": "no_data", … }`, never fabricate.
- Because coverage is bursty (Option B), surface data freshness/gaps so the agent can caveat answers.
- **Coverage + confidence ride in the same return:** an agent answering "how
  did I sleep?" should not need to consult `get_status` first. Window-dependent
  returns include their own coverage info, and baseline-derived numbers carry
  `provisional: true` while the baseline has fewer than seven days of history.

## Tools

### `get_status()`
Pipeline health, per-host sync freshness, and coverage gaps.
```json
{
  "last_reading_utc": "2042-03-17T10:12:34Z",
  "last_sync_by_host": { "synthetic-laptop": "2042-03-17T10:12:34Z" },
  "coverage_gaps_24h": [
    { "start": "2042-03-16T15:20:00Z", "end": "2042-03-16T16:05:00Z" }
  ],
  "rows": { "hr_samples": 123456, "rr_intervals": 98765 },
  "battery_pct": 57,
  "battery_raw": 321,
  "battery_low": false,
  "battery_note": "battery_pct is self-calibrated against the highest charge level observed so far, not a manufacturer-calibrated 0-100% reading; treat it as relative, not absolute, until more full-charge cycles have been seen.",
  "db_path": "/private/local/path/whoop.db",
  "schema_version": 14
}
```

### `get_latest_recovery()`
```json
{
  "date": "2042-03-17",
  "recovery_proxy": 67,
  "rmssd_ms": 47.3,
  "hrv_baseline_ms": 49.8,
  "rhr_bpm": 63.0,
  "sleep_minutes": 388,
  "provisional": false,
  "coverage_pct": 0.97,
  "confidence": { "level": "high", "actionable": true, "reasons": [] },
  "note": "Independent proxy; not a WHOOP recovery score."
}
```
`provisional` is `true` while `hrv_baseline_ms`/`recovery_proxy` are built on
3–6 days of history, `false` once at least 7 days exist, and `null` when there
is no baseline. `coverage_pct` is the fraction of the day's window with live
capture; below `coverage_low_threshold` (0.95), `note` gets an "at least"/floor
caveat appended.

**Decision-quality gate:** `recovery_proxy` is withheld (`null`) unless capture
coverage is at least 75%, a detected sleep window is at least 180 minutes, all
recovery inputs exist, and the baseline is non-provisional. The underlying
computed value remains in `metrics_daily` for audit/recomputation.
`confidence.reasons` names every failed gate so an agent can explain why it
must not adapt a workout from that day.

### `get_hrv(start_date, end_date)`
Daily HRV series over an inclusive range.
```json
{
  "range": { "start": "2042-03-10", "end": "2042-03-17" },
  "series": [
    { "date": "2042-03-10", "rmssd_ms": 45.6, "sdnn_ms": 62.7 },
    { "date": "2042-03-11", "rmssd_ms": 52.4, "sdnn_ms": 68.9 }
  ],
  "note": "RMSSD computed over detected sleep windows."
}
```

### `get_sleep(date)`
```json
{
  "date": "2042-03-17",
  "sleep_minutes": 388,
  "coverage_pct": 0.97,
  "segments": [ { "start": "2042-03-16T23:52:00Z", "end": "2042-03-17T06:20:00Z" } ],
  "note": "Coarse HR-based detection; no accelerometer input and no clinical sleep staging."
}
```

### `get_strain(date)`
```json
{
  "date": "2042-03-17",
  "strain_proxy": 9.7,
  "time_in_zones_min": { "z1": 82, "z2": 33, "z3": 14, "z4": 4, "z5": 0 },
  "coverage_pct": 0.97,
  "confidence": { "level": "high", "actionable": true, "reasons": [] },
  "note": "TRIMP-style proxy on a custom scale; not WHOOP strain (0-21). HRmax comes from an explicit configured value or configured age/date of birth."
}
```

The derived strain value is withheld below 75% day coverage. Computation also
withholds strain when no explicit HRmax, age, or date-of-birth context is
configured; it never silently assumes an owner profile. Imported workout
envelopes remain visible as independently sourced context.

### `query_readings(metric, start_utc, end_utc, limit?)`
Raw time-series access for ad-hoc questions / charts.
- `metric` ∈ `{"hr","rr","spo2","temp","hr_apple"}`, `limit` default 5000. `hr_apple` reads `imported_hr_samples` — Apple-export HR for direct WHOOP-vs-Watch overlay.
```json
{ "metric": "hr", "count": 731, "samples": [ { "ts": "2042-03-17T06:00:01Z", "value": 64 } ] }
```

### Workout logbook and plan tools (schema v14)

- `get_workout_history(start_date, end_date, limit=50)` — compact canonical sessions with duration, source, exercise count, work-set count, and rest-event count.
- `search_exercises(query="", limit=20)` — discover exact canonical exercise names, session counts, and last-performed dates.
- `get_exercise_history(exercise_name, limit=20)` — recent work sets for an exact canonical exercise plus max weight and a conservative Epley estimated 1RM (only 1-12 rep sets).
- `get_training_summary(start_date, end_date)` — session frequency, work sets, reps, timed work, volume grouped by declared unit, and top exercises.
- `get_active_plan()` — the one current, versioned intended plan: weekly sport/recovery/strength slots and exercise prescriptions. It returns no data until a plan is activated, and it never records completion or changes the plan.

Strong CSV does not declare its configured weight unit. Imports must preserve
that uncertainty or receive an explicit reviewed unit; tools still group volume
by declared unit and return the provenance note. These are read tools only —
plan mutation and workout logging require the confirmed-write workflow.

### Prospective exercise library (schema v14)

- `search_exercise_library(query="", equipment=null, target=null, body_part=null, review_status="all", limit=20)` — searches external prospective metadata and returns compact equipment/muscle/review fields. It never promotes, plans, or logs an exercise.
- `get_exercise_library_entry(external_id)` — returns one entry's English instructions, structured steps, muscle/equipment metadata, source URL/license, and review/canonical state. Missing IDs return explicit `no_data`.

Both are read-only and state prominently that unreviewed entries are discovery material—not performed history, a recommendation, or a canonical identity. The local review CLI is the only promotion surface:

```bash
.venv/bin/python -m whoop_local.exercise_library.cli import --json-file exercises.json
.venv/bin/python -m whoop_local.exercise_library.cli search --query row --equipment cable
.venv/bin/python -m whoop_local.exercise_library.cli show EXTERNAL_ID
.venv/bin/python -m whoop_local.exercise_library.cli approve-existing EXTERNAL_ID --canonical-name "Existing Name"
.venv/bin/python -m whoop_local.exercise_library.cli approve-new EXTERNAL_ID --canonical-name "Intentional New Name"
# or: .venv/bin/python -m whoop_local.exercise_library.cli reject EXTERNAL_ID --notes "Reason"
```

Import is metadata-only and idempotent. It validates the entire JSON array before writing, stores provenance/content hashes, excludes media fields, and preserves review decisions on re-import. Reviews are immutable in v1. `approve_new` refuses a normalized name that already exists and directs the owner to `approve-existing`; fuzzy similarity never auto-approves.

### Manual workout logging (local CLI—not an MCP write tool)

Manual logging is deliberately a two-step local operation. `prepare` validates and stores a pending draft but writes **no** canonical workout rows. Review it with `show`, then a separate explicit `confirm` command materializes the workout once. `cancel` discards a pending draft. Confirmed logs are immutable through this interface.

```bash
.venv/bin/python -m whoop_local.manual_logging.cli prepare --json-file workout.json
.venv/bin/python -m whoop_local.manual_logging.cli show DRAFT_ID
.venv/bin/python -m whoop_local.manual_logging.cli confirm DRAFT_ID
```

`workout.json` must contain an exact catalog exercise name, timezone-aware ISO
timestamps, and an explicit supported unit (`kg` or `lb`) for weighted sets.
Example:

```json
{
  "name": "Lower strength",
  "started_at": "2042-03-21T17:30:00-04:00",
  "ended_at": "2042-03-21T18:40:00-04:00",
  "exercises": [{
    "name": "Squat (Barbell)",
    "sets": [{"weight": 55, "weight_unit": "kg", "reps": 7, "rir": 3}]
  }]
}
```

Unknown exercise variants, timezone-less timestamps, and weighted entries with
missing or unsupported units are rejected before a draft is saved. The MCP
server remains read-only in this stage.

### Coaching proposals

`get_coaching_proposals(status="pending", limit=20)` exposes a proposal's
frozen evidence, confidence, rationale, and exact intended plan change; it
cannot resolve a proposal.

The local proposal CLI is deliberately separate from the read tool:

```bash
.venv/bin/python -m whoop_local.coaching.cli propose-weight "Lower strength" "Squat (Barbell)"
.venv/bin/python -m whoop_local.coaching.cli show PROPOSAL_ID
.venv/bin/python -m whoop_local.coaching.cli accept PROPOSAL_ID
# or: .venv/bin/python -m whoop_local.coaching.cli reject PROPOSAL_ID
```

The initial rule only proposes a unit-preserving `kg` or `lb` load increase
after the two latest completed sessions meet every target set and rep with
recorded RIR ≥2. It
freezes the source sessions and explicitly states that no biometric signal was
used. Any future recovery-based proposal must satisfy the actionable-confidence
gate. Acceptance archives the old plan and creates a new version; rejection
leaves it untouched.

### Training decision tools

- `get_plan_adherence(start_date, end_date)` — expands the active plan into dated slots, matches strength against the detailed logbook and conditioning against labeled imported workouts, and excludes optional recovery from the denominator. Range maximum: 93 days.
- `get_training_guidance(date)` — returns the planned session plus non-mutating advice. Recovery modifies the advice only when the confidence gate marks it actionable; low actionable recovery reduces strength to about 75% of sets/90% of load or makes sport easy/optional.
- `get_weekly_training_review(week_start)` — Monday-start adherence, workload, missed/unlogged slots, and the average of actionable recovery days only. Current weeks stop at today, so future sessions are never called missed.

### Minimum-disclosure relay tools

- `get_telegram_training_guidance(date)` wraps the full guidance contract into
  `{date, text, relay_contract}`. `text` is deterministic and includes exact
  state, prescriptions, plan/RIR/safety notes, and recovery confidence/advice.
- `get_telegram_weekly_review(week_start)` wraps the full weekly review into
  `{week_start, text, relay_contract}`, retaining completion-source caveats.

These remain read-only. They exist so an owner-only chat model receives only
the final minimum-useful answer rather than verbose structured health/history
payloads, and so it cannot reinterpret completion by querying overlapping
sources. The accepted BROC route exposes only these two main Doop relays; the
underlying full tools remain available locally but are filtered from that
agent. Deterministic safety/offline tools live on a separate database-free
surface as diagnostic, non-promoted fallbacks. Promoted safety/offline
acceptance requires the trusted pre-model gate with zero model tokens and zero
tool calls; a model-mediated fallback-tool call does not pass.

Both relays are read-only. Guidance never edits a plan, logs a workout, or
resolves a proposal. Pain, illness, severe fatigue, dizziness, chest pain, or
acute injury overrides generated guidance before model inference.

## Optional resource

### `daily_summary` (resource)
Compact markdown summary of the latest day: recovery, HRV, resting HR, sleep,
and capture freshness. It does **not** include strain.

## Capability order

1. `get_status` + `query_readings` are the raw-storage smoke test.
2. `get_hrv` + `get_latest_recovery` require daily metric computation.
3. `get_sleep` + `get_strain` require their matching derived data.

## Coverage and attribution contract

These behaviors are part of the current schema-v14 contract:

- **Day attribution:** date args/returns follow local noon-to-noon windows,
  sleep credited to the wake-up date (see `METRICS.md`). "Last night's sleep"
  means today's row.
- **`get_sleep` / `get_latest_recovery`:** `coverage_pct` is the fraction of
  the window with live capture. When it is below `coverage_low_threshold`, the
  `note` gets a floor/"at least" caveat.
- **`get_latest_recovery`:** baseline-derived fields may be provisional: true
  with 3–6 days of history, false with at least 7 days, or null with no baseline.
  The flag does not apply to the raw daily series from `get_hrv`.
- **`get_status`:** `battery_pct` is self-calibrated against the highest raw
  charge level observed so far, not a manufacturer-calibrated value.
- **`daily_summary`:** includes a plain-language explanation of the recovery
  inputs and any withholding reasons, so the proxy is auditable rather than a
  mystery score. **Implemented.**

## Apple Health second-observer contract

Imported Apple Health data appears *beside* WHOOP-derived numbers, always
labeled and never blended:

- **`get_sleep`:** when `imported_sleep` rows exist for the date, adds `"imported_sleep": {"imported_sleep_minutes", "basis" ("asleep_stages"|"inbed"), "sources"}` + an `imported_note` naming the source. Minutes are the merged union across sources (overlapping iPhone+Watch records don't double-count). WHOOP `sleep_minutes` stays exactly what our own pipeline detected — `null` stays `null`. Imported data alone is enough to avoid `no_data`.
- **`get_strain`:** adds `"workouts": [{start, end, activity, duration_min, source_name}]` for the attributed local date plus a `workouts_note`. This is context for validating the strain proxy, not an input to it. Workouts alone avoid `no_data` with `strain_proxy: null`.
- **`query_readings`:** new metric `hr_apple` (see above).

## Implementation and user-local registration

All tools + the `daily_summary` resource implemented in `src/whoop_local/mcp_server/`:
- `queries.py` — pure, testable query functions over a `sqlite3.Connection` (no MCP/transport dependency; see `tests/test_mcp_queries.py`).
- `server.py` — FastMCP wiring (stdio transport). `get_strain` returns a
  computed value or `{"status": "no_data"}` when the date has no computed row
  — never fabricated.

The showcase repository deliberately contains no client registration file.
Register the command in your MCP client's **user-local** configuration:
```json
{
  "command": "uv",
  "args": ["run", "doop-mcp"]
}
```
Keep that client configuration private. Set the database location in your
private Doop config or through `DOOP_DB_PATH`; see `USING_THIS_CODE.md` for the
complete supported workflow.

To run/test standalone: `uv run doop-mcp` (blocks on stdio — Ctrl+C to
stop; it is mainly useful to confirm startup rather than for interactive use).
