# SCHEMA.md

> The SQLite data model. Single source of truth for tables and columns. Evolve via migrations — when you change it, bump `schema_version` in `meta` and document the change in the pull request and this file.

## Principles

- **Raw before derived.** `raw_packets` is append-only and never parsed-away. Everything else can be regenerated from it.
- **Typed time-series tables**, one row per sample, indexed on timestamp.
- **UTC everywhere.** Recommend integer epoch-ms for cheap range queries.
- **Multi-host aware.** Every row carries a validated `source_host` label
  (platform defaults: `mac`, `linux`, or `pi`; custom labels are allowed) so
  capture sources can reconcile. Historical records also carry a **device-side
  timestamp** for dedup.
- Personal health data — the default `.db` lives in the platform application-data directory outside the checkout; configuration may point it to another private local path.

## Two kinds of timestamp

- `ts_utc` — the sample time used by compute. For live rows this is host receipt time; for decoded history rows it is the band's measurement time. Raw transport receipt time always remains in `raw_packets.ts_utc`.
- `device_ts_utc` — the timestamp the *band* assigns to a buffered record (present for history-sync rows; NULL for pure live samples). Dedup uses this so the same buffered record downloaded by both hosts collapses to one row.

## Tables

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- current: ('schema_version', '14')
-- every database also has one stable ('dataset_id', '<uuid>') identity

-- Append-only raw BLE notifications / downloaded frames. Never deleted.
CREATE TABLE raw_packets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,       -- epoch ms received
    source_uuid   TEXT    NOT NULL,       -- characteristic UUID
    source_host   TEXT    NOT NULL,       -- validated per-host source label
    origin        TEXT    NOT NULL DEFAULT 'live', -- 'live' | 'history'
    payload_hex   TEXT    NOT NULL,       -- verbatim bytes, hex
    notes         TEXT
);
CREATE INDEX idx_raw_ts ON raw_packets(ts_utc);

CREATE TABLE hr_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,               -- set for history-sync rows
    hr_bpm        INTEGER NOT NULL,
    source_host   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'hrm', -- 'hrm' | 'whoop'
    UNIQUE(device_ts_utc, source)        -- dedup buffered records; NULLs never collide
);
CREATE INDEX idx_hr_ts ON hr_samples(ts_utc);

CREATE TABLE rr_intervals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    rr_ms         REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'hrm',
    rr_index      INTEGER               -- stable slot within one history record
);
CREATE INDEX idx_rr_ts ON rr_intervals(ts_utc);
CREATE UNIQUE INDEX idx_rr_history_dedup
    ON rr_intervals(device_ts_utc, source, rr_index)
    WHERE device_ts_utc IS NOT NULL AND rr_index IS NOT NULL;

-- Durable identity for complete type-47 history records (D69, schema v13).
-- Raw notifications remain append-only; this table makes typed replays safe.
CREATE TABLE historical_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     INTEGER NOT NULL,
    device_ts_utc   INTEGER,
    record_counter  INTEGER,
    record_version  INTEGER,
    source_host     TEXT    NOT NULL,
    payload_hex     TEXT    NOT NULL UNIQUE,
    decoded         INTEGER NOT NULL DEFAULT 0 CHECK(decoded IN (0, 1))
);
CREATE INDEX idx_historical_records_device_ts
    ON historical_records(device_ts_utc);

CREATE TABLE spo2_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    spo2_pct      REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(device_ts_utc)
);
CREATE INDEX idx_spo2_ts ON spo2_samples(ts_utc);

CREATE TABLE temp_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    skin_temp_c   REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(device_ts_utc)
);
CREATE INDEX idx_temp_ts ON temp_samples(ts_utc);

CREATE TABLE sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT    NOT NULL,           -- 'sleep' | 'workout' | 'other'
    start_utc INTEGER NOT NULL,
    end_utc   INTEGER,
    source    TEXT    NOT NULL DEFAULT 'auto', -- 'auto' | 'manual'
    notes     TEXT
);
CREATE INDEX idx_sessions_start ON sessions(start_utc);

CREATE TABLE metrics_daily (
    date            TEXT PRIMARY KEY,     -- 'YYYY-MM-DD', local noon-to-noon window (D25)
    rmssd_ms        REAL,
    sdnn_ms         REAL,
    rhr_bpm         REAL,
    hrv_baseline_ms REAL,
    recovery_proxy  REAL,                 -- 0-100
    strain_proxy    REAL,                 -- TRIMP-style custom scale, not WHOOP 0-21 (D32, schema v5)
    time_in_zones_json TEXT,              -- JSON {"z1": minutes, ...} (D32, schema v5)
    sleep_minutes   INTEGER,
    computed_at     INTEGER,
    timezone        TEXT,                 -- IANA zone used for this row's day window (D25, schema v2)
    provisional     INTEGER,              -- 1 if hrv_baseline_ms/recovery_proxy used <7 days of history (D26/D30, schema v3)
    coverage_pct    REAL                  -- effective live + recovered-history coverage (D26/D31/D69)
);

-- Device identity per host (macOS UUID vs Pi MAC differ)
CREATE TABLE device (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host          TEXT,                   -- validated source label
    identifier    TEXT,                   -- peripheral UUID (mac) | BLE address (linux/pi)
    name          TEXT,
    firmware      TEXT,
    last_seen_utc INTEGER
);

-- Connection-drop tracking for the live collector (Phase 1 DoD: "log gaps")
CREATE TABLE capture_gaps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_host   TEXT    NOT NULL,
    gap_start_utc INTEGER NOT NULL,       -- disconnect detected
    gap_end_utc   INTEGER,                -- NULL while still disconnected
    reason        TEXT
);

-- Decoded proprietary battery event (Phase 3b, D28). raw_value is a fuel-gauge
-- counter, not a calibrated manufacturer percentage.
CREATE TABLE battery_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    raw_value     INTEGER NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(ts_utc, source_host)
);
CREATE INDEX idx_battery_ts ON battery_samples(ts_utc);

-- Capture-only replication ledger (D62, schema v12).
CREATE TABLE sync_ingest_rows (
    source_dataset_id TEXT    NOT NULL,
    source_table      TEXT    NOT NULL,
    source_row_id     INTEGER NOT NULL,
    canonical_row_id  INTEGER,
    imported_at       INTEGER NOT NULL,
    PRIMARY KEY(source_dataset_id, source_table, source_row_id)
);
CREATE INDEX idx_sync_ingest_imported ON sync_ingest_rows(imported_at);

-- History import (Phase 6, D33): daily summaries from an external source
-- (e.g. Apple Health export), kept separate from metrics_daily so imported
-- data is never confused with our own WHOOP-derived computation. Only
-- rhr_bpm feeds the rolling baseline, and only as a cold-start fallback
-- (see compute_daily.py _load_history) -- hrv_sdnn_ms is stored for
-- reference only (Apple's SDNN metric isn't our RMSSD).
CREATE TABLE imported_daily_metrics (
    date         TEXT PRIMARY KEY,
    rhr_bpm      REAL,
    hrv_sdnn_ms  REAL,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL
);

-- Apple Watch comparison data (schema v6, D38). Second observer:
-- fills reporting gaps (nights/workouts away from the collector) and backs
-- Phase 2's "sanity-check against another HR source". Surfaced beside
-- WHOOP-derived numbers by the MCP layer, never written into metrics_daily.

-- Raw sleep segments from HKCategoryTypeIdentifierSleepAnalysis. `date` is
-- D25-attributed (noon-to-noon window of the segment's END = wake-up date).
-- stage: inbed | asleep_unspecified | asleep_core | asleep_deep | asleep_rem | awake.
CREATE TABLE imported_sleep (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL,
    start_utc    INTEGER NOT NULL,
    end_utc      INTEGER NOT NULL,
    stage        TEXT    NOT NULL,
    source_name  TEXT,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL,
    UNIQUE(start_utc, end_utc, stage, source_name)
);
CREATE INDEX idx_imported_sleep_date ON imported_sleep(date);

-- <Workout> elements; `date` is the D25-attributed day of the workout's end.
CREATE TABLE imported_workouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL,
    start_utc    INTEGER NOT NULL,
    end_utc      INTEGER NOT NULL,
    activity     TEXT    NOT NULL,
    duration_min REAL,
    source_name  TEXT,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL,
    UNIQUE(start_utc, activity, source_name)
);
CREATE INDEX idx_imported_workouts_date ON imported_workouts(date);

-- Per-sample export HR, bounded by the importer's --hr-since cutoff
-- (HR records are the bulk of a multi-GB export). Queryable as metric
-- 'hr_apple' for direct WHOOP-vs-Watch overlay where captures overlap.
CREATE TABLE imported_hr_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       INTEGER NOT NULL,
    hr_bpm       REAL    NOT NULL,
    source_name  TEXT,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL,
    UNIQUE(ts_utc, source_name)
);
CREATE INDEX idx_imported_hr_ts ON imported_hr_samples(ts_utc);

-- Canonical workout logbook (schema v7, D40). Detailed Strong history and
-- future manual/agent logging share these normalized tables.
CREATE TABLE exercise_catalog (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at      INTEGER NOT NULL
);

-- Prospective exercise reference library (schema v11, D60/D61). Importing an
-- entry does not make it canonical, planned, or performed. Restricted media
-- paths/assets are deliberately excluded.
CREATE TABLE external_exercises (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source                 TEXT    NOT NULL,
    source_external_id     TEXT    NOT NULL,
    name                   TEXT    NOT NULL,
    normalized_name        TEXT    NOT NULL,
    body_part              TEXT,
    equipment              TEXT,
    target                 TEXT,
    muscle_group           TEXT,
    secondary_muscles_json TEXT    NOT NULL DEFAULT '[]',
    instructions_en        TEXT,
    instruction_steps_json TEXT    NOT NULL DEFAULT '[]',
    source_url             TEXT    NOT NULL,
    source_license         TEXT    NOT NULL,
    content_hash           TEXT    NOT NULL,
    imported_at            INTEGER NOT NULL,
    UNIQUE(source, source_external_id)
);
CREATE INDEX idx_external_exercises_name ON external_exercises(normalized_name);
CREATE INDEX idx_external_exercises_filters ON external_exercises(body_part, equipment, target);

-- One immutable explicit review per external entry. Only approved rows point
-- at exercise_catalog. approve_new deliberately creates the canonical row;
-- imports and searches never do.
CREATE TABLE external_exercise_reviews (
    external_exercise_id INTEGER PRIMARY KEY
        REFERENCES external_exercises(id) ON DELETE CASCADE,
    status               TEXT    NOT NULL, -- approved_existing | approved_new | rejected
    exercise_id          INTEGER REFERENCES exercise_catalog(id),
    review_notes         TEXT,
    reviewed_at          INTEGER NOT NULL,
    CHECK(
        (status IN ('approved_existing', 'approved_new') AND exercise_id IS NOT NULL)
        OR (status = 'rejected' AND exercise_id IS NULL)
    )
);
CREATE INDEX idx_external_exercise_reviews_exercise ON external_exercise_reviews(exercise_id);

CREATE TABLE workout_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc     INTEGER NOT NULL,
    ended_at_utc       INTEGER,
    local_date         TEXT NOT NULL,
    name               TEXT NOT NULL,
    source             TEXT NOT NULL,
    source_external_id TEXT NOT NULL,
    notes              TEXT,
    imported_at        INTEGER NOT NULL,
    UNIQUE(source, source_external_id)
);

CREATE TABLE workout_exercises (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_id        INTEGER NOT NULL REFERENCES exercise_catalog(id),
    exercise_order     INTEGER NOT NULL,
    notes              TEXT,
    UNIQUE(workout_session_id, exercise_order)
);

CREATE TABLE workout_sets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_exercise_id INTEGER NOT NULL REFERENCES workout_exercises(id) ON DELETE CASCADE,
    set_order           INTEGER NOT NULL,
    sequence_order      INTEGER,
    set_type            TEXT NOT NULL DEFAULT 'work', -- work | rest
    source_set_label    TEXT,
    weight              REAL,
    weight_unit         TEXT NOT NULL DEFAULT 'unknown',
    reps                REAL,
    distance            REAL,
    seconds             REAL,
    rpe                 REAL,
    rir                 REAL,
    notes               TEXT,
    source_row_hash     TEXT NOT NULL UNIQUE
);

-- Versioned intended plans (schema v8, D46). These are deliberately separate
-- from workout_sessions: a prescription never implies completion.
CREATE TABLE training_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL, -- draft | active | archived
    starts_on TEXT,
    ends_on TEXT,
    source TEXT NOT NULL,
    notes TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE plan_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_plan_id INTEGER NOT NULL REFERENCES training_plans(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL, -- Monday=0 ... Sunday=6
    display_order INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL, -- strength | sport | recovery
    intensity TEXT NOT NULL, -- easy | moderate | hard | heavy
    estimated_minutes INTEGER,
    optional INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE(training_plan_id, display_order)
);

CREATE TABLE plan_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_workout_id INTEGER NOT NULL REFERENCES plan_workouts(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES exercise_catalog(id),
    exercise_order INTEGER NOT NULL,
    block_label TEXT,
    sets_target INTEGER NOT NULL,
    reps_min INTEGER,
    reps_max INTEGER,
    tempo TEXT,
    rest_seconds INTEGER,
    starting_weight REAL,
    weight_unit TEXT NOT NULL DEFAULT 'bodyweight',
    substitution_note TEXT,
    notes TEXT,
    UNIQUE(plan_workout_id, exercise_order)
);

-- Confirm-before-write manual logging (schema v9, D47). A pending draft is
-- not a completed workout; only an explicit confirmation creates logbook rows.
CREATE TABLE manual_workout_drafts (
    id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL, -- pending | confirmed | cancelled
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    workout_session_id INTEGER REFERENCES workout_sessions(id),
    CHECK(
        (status = 'confirmed' AND confirmed_at IS NOT NULL AND workout_session_id IS NOT NULL)
        OR (status != 'confirmed' AND confirmed_at IS NULL AND workout_session_id IS NULL)
    )
);

-- Evidence-backed coaching suggestions (schema v10, D48). The pending
-- proposal freezes its evidence and proposed diff; acceptance produces a new
-- training_plans version rather than editing the originating plan.
CREATE TABLE coaching_proposals (
    id TEXT PRIMARY KEY,
    training_plan_id INTEGER NOT NULL REFERENCES training_plans(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL, -- pending | accepted | rejected
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    change_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    resulting_plan_id INTEGER REFERENCES training_plans(id)
);

-- Private monthly Excel source evidence (schema v14, D82). These rows are
-- intentionally separate from metrics, workouts, plans, and coaching.
CREATE TABLE monthly_report_imports (
    id                   TEXT PRIMARY KEY,
    report_type          TEXT NOT NULL,
    report_period        TEXT NOT NULL, -- YYYY-MM
    revision             INTEGER NOT NULL,
    status               TEXT NOT NULL, -- active | superseded
    source_filename      TEXT NOT NULL,
    stored_relpath       TEXT NOT NULL UNIQUE,
    content_sha256       TEXT NOT NULL,
    size_bytes           INTEGER NOT NULL,
    mapping_id           TEXT NOT NULL,
    mapping_version      INTEGER NOT NULL,
    mapping_hash         TEXT NOT NULL,
    mapping_json         TEXT NOT NULL, -- canonical versioned mapping snapshot
    sheet_name           TEXT NOT NULL,
    row_count            INTEGER NOT NULL,
    rows_digest          TEXT NOT NULL,
    parser_version       INTEGER NOT NULL,
    supersedes_import_id TEXT REFERENCES monthly_report_imports(id),
    imported_at          INTEGER NOT NULL,
    UNIQUE(report_type, report_period, revision),
    UNIQUE(report_type, report_period, content_sha256, mapping_hash)
);
CREATE UNIQUE INDEX idx_monthly_report_active
    ON monthly_report_imports(report_type, report_period)
    WHERE status = 'active';
-- An insert trigger rejects reuse of one (mapping_id, mapping_version) with a
-- different mapping_hash, including concurrent applies.

CREATE TABLE monthly_report_rows (
    import_id         TEXT NOT NULL
        REFERENCES monthly_report_imports(id) ON DELETE CASCADE,
    source_row_number INTEGER NOT NULL,
    row_key           TEXT NOT NULL,
    row_hash          TEXT NOT NULL,
    data_json         TEXT NOT NULL,
    PRIMARY KEY(import_id, source_row_number),
    UNIQUE(import_id, row_key)
);
```

## Dedup strategy (multi-host + history sync)

- **Live samples** (no `device_ts_utc`): tagged by `source_host`; overlaps are rare because hosts connect at different times. Acceptable to keep both if they ever coincide.
- **History-sync rows** (`device_ts_utc` set): the `UNIQUE(device_ts_utc, …)` constraints + `INSERT OR IGNORE` collapse the same buffered record pulled by both hosts into one.
- **Pi snapshot retries:** each DB has a stable `meta.dataset_id`; `sync_ingest_rows` records `(source_dataset_id, source_table, source_row_id)`, so importing the same or an overlapping Pi snapshot repeatedly is a no-op. Only raw/capture tables replicate. Open gaps wait until closed.
- **Monthly report retries:** exact workbook bytes plus the frozen mapping hash
  identify an import. Reapplying them is a no-op; changed content for an active
  type/month requires an explicit supersession and preserves both revisions.
- Do dedup **at ingest**, not with destructive cleanup jobs.

## Notes / decisions to confirm

- ~~**Local "day" boundary for `metrics_daily`:**~~ **Resolved — D25:** local noon-to-noon, sleep credited to the wake-up date. See METRICS.md → "Day attribution".
- **Canonical DB across hosts:** resolved by D62. The Pi owns a local capture DB and transferable SQLite-backup snapshot; the Mac imports it into the canonical DB. Never open SQLite over a network share.
- **Migrations:** no dedicated `migrations/` directory yet — schema changes
  remain additive and idempotent in `db/schema.py`; `meta.schema_version` is
  currently 14 (v11 prospective exercise library; v12 dataset identity + sync
  ledger; v13 durable history-record identity/RR dedup; v14 monthly report
  evidence/revisions). Add ordered transactional migrations before the next
  non-additive change.
