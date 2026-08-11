"""SQLite schema for the local WHOOP dataset. Mirrors SCHEMA.md — keep them in sync."""

import sqlite3
import uuid
from pathlib import Path

from whoop_local.security import secure_directory, secure_sqlite_files

SCHEMA_VERSION = "14"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS raw_packets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    source_uuid   TEXT    NOT NULL,
    source_host   TEXT    NOT NULL,
    origin        TEXT    NOT NULL DEFAULT 'live',
    payload_hex   TEXT    NOT NULL,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_packets(ts_utc);

CREATE TABLE IF NOT EXISTS hr_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    hr_bpm        INTEGER NOT NULL,
    source_host   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'hrm',
    UNIQUE(device_ts_utc, source)
);
CREATE INDEX IF NOT EXISTS idx_hr_ts ON hr_samples(ts_utc);

CREATE TABLE IF NOT EXISTS rr_intervals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    rr_ms         REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'hrm',
    rr_index      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rr_ts ON rr_intervals(ts_utc);

-- Phase 4 (D69): one durable row per complete type-47 record. payload_hex is
-- globally unique so a crash-before-ACK replay, or the same buffered record
-- downloaded by both sync stations, cannot duplicate typed HR/RR rows.
CREATE TABLE IF NOT EXISTS historical_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   INTEGER NOT NULL,
    device_ts_utc INTEGER,
    record_counter INTEGER,
    record_version INTEGER,
    source_host   TEXT    NOT NULL,
    payload_hex   TEXT    NOT NULL UNIQUE,
    decoded       INTEGER NOT NULL DEFAULT 0 CHECK(decoded IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_historical_records_device_ts
    ON historical_records(device_ts_utc);

CREATE TABLE IF NOT EXISTS spo2_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    spo2_pct      REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(device_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_spo2_ts ON spo2_samples(ts_utc);

CREATE TABLE IF NOT EXISTS temp_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    device_ts_utc INTEGER,
    skin_temp_c   REAL    NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(device_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_temp_ts ON temp_samples(ts_utc);

CREATE TABLE IF NOT EXISTS sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT    NOT NULL,
    start_utc INTEGER NOT NULL,
    end_utc   INTEGER,
    source    TEXT    NOT NULL DEFAULT 'auto',
    notes     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_utc);

CREATE TABLE IF NOT EXISTS metrics_daily (
    date            TEXT PRIMARY KEY,
    rmssd_ms        REAL,
    sdnn_ms         REAL,
    rhr_bpm         REAL,
    hrv_baseline_ms REAL,
    recovery_proxy  REAL,
    strain_proxy    REAL,
    time_in_zones_json TEXT, -- D32: JSON {"z1": minutes, ...} -- see get_strain
    sleep_minutes   INTEGER,
    computed_at     INTEGER,
    timezone        TEXT,
    provisional     INTEGER,  -- D26: 1 if hrv_baseline_ms/recovery_proxy used < baseline_min_days of history
    coverage_pct    REAL      -- D26/D69: effective live + recovered-history coverage
);

CREATE TABLE IF NOT EXISTS device (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host          TEXT,
    identifier    TEXT,
    name          TEXT,
    firmware      TEXT,
    last_seen_utc INTEGER
);

-- Tracks connection gaps (capture drops) for the current host.
CREATE TABLE IF NOT EXISTS capture_gaps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_host   TEXT    NOT NULL,
    gap_start_utc INTEGER NOT NULL,
    gap_end_utc   INTEGER,
    reason        TEXT
);

-- Decoded proprietary battery event (D28). raw_value is the undecoded
-- fuel-gauge counter from event=3 on the events channel; zero preceded an
-- observed shutdown and the value rose during charging.
CREATE TABLE IF NOT EXISTS battery_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        INTEGER NOT NULL,
    raw_value     INTEGER NOT NULL,
    source_host   TEXT    NOT NULL,
    UNIQUE(ts_utc, source_host)
);
CREATE INDEX IF NOT EXISTS idx_battery_ts ON battery_samples(ts_utc);

-- D62: idempotent capture-only replication from a Pi-owned SQLite snapshot
-- into the Mac-canonical database. The source database has its own stable
-- meta.dataset_id; this ledger prevents a repeated snapshot from duplicating
-- any source row. canonical_row_id is informational because an existing
-- device-timestamp uniqueness constraint may cause INSERT OR IGNORE to win.
CREATE TABLE IF NOT EXISTS sync_ingest_rows (
    source_dataset_id TEXT    NOT NULL,
    source_table      TEXT    NOT NULL,
    source_row_id     INTEGER NOT NULL,
    canonical_row_id  INTEGER,
    imported_at       INTEGER NOT NULL,
    PRIMARY KEY(source_dataset_id, source_table, source_row_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_ingest_imported
    ON sync_ingest_rows(imported_at);

-- History import (Phase 6, D33): daily summaries from an external source
-- (e.g. Apple Health export), kept separate from metrics_daily so imported
-- data is never confused with our own WHOOP-derived computation (D6: raw
-- before derived, and never blend measurement methodologies silently).
-- Only rhr_bpm currently feeds the rolling baseline (see compute_daily.py
-- _load_history) -- hrv_sdnn_ms is Apple's SDNN metric, not comparable to
-- our RMSSD-based HRV baseline, so it's stored for reference only.
CREATE TABLE IF NOT EXISTS imported_daily_metrics (
    date         TEXT PRIMARY KEY,
    rhr_bpm      REAL,
    hrv_sdnn_ms  REAL,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL
);

-- Apple Watch comparison data (schema v6, D38). Same separation principle as
-- imported_daily_metrics: these tables sit BESIDE the WHOOP-derived pipeline
-- as a second observer -- for gap-filling reports and validating our proxies
-- (Phase 2 DoD's "sanity-check against another HR source") -- and are never
-- silently blended into metrics_daily.

-- Raw sleep segments from HKCategoryTypeIdentifierSleepAnalysis. `date` is the
-- D25-attributed local day (the noon-to-noon window the segment's END falls in,
-- i.e. credited to the wake-up date). Stages: inbed, asleep_unspecified,
-- asleep_core, asleep_deep, asleep_rem, awake (per-record; older exports only
-- have inbed).
CREATE TABLE IF NOT EXISTS imported_sleep (
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
CREATE INDEX IF NOT EXISTS idx_imported_sleep_date ON imported_sleep(date);

-- <Workout> elements. `date` is the D25-attributed local day of the workout's end.
CREATE TABLE IF NOT EXISTS imported_workouts (
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
CREATE INDEX IF NOT EXISTS idx_imported_workouts_date ON imported_workouts(date);

-- Per-sample HR from the export (HKQuantityTypeIdentifierHeartRate), bounded
-- by the importer's --hr-since cutoff (millions of rows exist in a full export).
-- Enables direct WHOOP-vs-Watch HR overlay where captures overlap.
CREATE TABLE IF NOT EXISTS imported_hr_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       INTEGER NOT NULL,
    hr_bpm       REAL    NOT NULL,
    source_name  TEXT,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL,
    UNIQUE(ts_utc, source_name)
);
CREATE INDEX IF NOT EXISTS idx_imported_hr_ts ON imported_hr_samples(ts_utc);

-- Canonical workout logbook (schema v7, D40). Imported and future manually
-- logged workouts share this model; provenance stays explicit on the session.
CREATE TABLE IF NOT EXISTS exercise_catalog (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT    NOT NULL,
    normalized_name TEXT    NOT NULL UNIQUE,
    created_at      INTEGER NOT NULL
);

-- Prospective exercise library (schema v11, D60/D61). External metadata is
-- reference material, never proof of performance and never canonical merely
-- because it was imported. Media fields are deliberately not stored.
CREATE TABLE IF NOT EXISTS external_exercises (
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
CREATE INDEX IF NOT EXISTS idx_external_exercises_name
    ON external_exercises(normalized_name);
CREATE INDEX IF NOT EXISTS idx_external_exercises_filters
    ON external_exercises(body_part, equipment, target);

-- An external entry becomes usable by plans/logging only after one explicit
-- review. Approved entries point at the canonical catalog; rejected entries
-- remain reference history and cannot accidentally be promoted on re-import.
CREATE TABLE IF NOT EXISTS external_exercise_reviews (
    external_exercise_id INTEGER PRIMARY KEY
        REFERENCES external_exercises(id) ON DELETE CASCADE,
    status               TEXT    NOT NULL
        CHECK(status IN ('approved_existing', 'approved_new', 'rejected')),
    exercise_id          INTEGER REFERENCES exercise_catalog(id),
    review_notes         TEXT,
    reviewed_at          INTEGER NOT NULL,
    CHECK(
        (status IN ('approved_existing', 'approved_new') AND exercise_id IS NOT NULL)
        OR (status = 'rejected' AND exercise_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_external_exercise_reviews_exercise
    ON external_exercise_reviews(exercise_id);

CREATE TABLE IF NOT EXISTS workout_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc     INTEGER NOT NULL,
    ended_at_utc       INTEGER,
    local_date         TEXT    NOT NULL,
    name               TEXT    NOT NULL,
    source             TEXT    NOT NULL, -- strong_csv | manual | agent | apple_health
    source_external_id TEXT    NOT NULL,
    notes              TEXT,
    imported_at        INTEGER NOT NULL,
    UNIQUE(source, source_external_id)
);
CREATE INDEX IF NOT EXISTS idx_workout_sessions_started ON workout_sessions(started_at_utc);
CREATE INDEX IF NOT EXISTS idx_workout_sessions_date ON workout_sessions(local_date);

CREATE TABLE IF NOT EXISTS workout_exercises (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_id        INTEGER NOT NULL REFERENCES exercise_catalog(id),
    exercise_order     INTEGER NOT NULL,
    notes              TEXT,
    UNIQUE(workout_session_id, exercise_order)
);
CREATE INDEX IF NOT EXISTS idx_workout_exercises_session ON workout_exercises(workout_session_id);
CREATE INDEX IF NOT EXISTS idx_workout_exercises_exercise ON workout_exercises(exercise_id);

CREATE TABLE IF NOT EXISTS workout_sets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_exercise_id INTEGER NOT NULL REFERENCES workout_exercises(id) ON DELETE CASCADE,
    set_order           INTEGER NOT NULL,
    sequence_order      INTEGER,
    set_type            TEXT    NOT NULL DEFAULT 'work', -- work | rest
    source_set_label    TEXT,
    weight              REAL,
    weight_unit         TEXT    NOT NULL DEFAULT 'unknown',
    reps                REAL,
    distance            REAL,
    seconds             REAL,
    rpe                 REAL,
    rir                 REAL,
    notes               TEXT,
    source_row_hash     TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_workout_sets_exercise ON workout_sets(workout_exercise_id);

-- Versioned, read-only training plans (schema v8, D46). A plan is distinct
-- from performed workouts: it records an intended schedule and prescriptions,
-- never claims a set was completed.
CREATE TABLE IF NOT EXISTS training_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    status      TEXT    NOT NULL CHECK(status IN ('draft', 'active', 'archived')),
    starts_on   TEXT,
    ends_on     TEXT,
    source      TEXT    NOT NULL,
    notes       TEXT,
    created_at  INTEGER NOT NULL,
    UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_training_plans_status ON training_plans(status);

CREATE TABLE IF NOT EXISTS plan_workouts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    training_plan_id  INTEGER NOT NULL REFERENCES training_plans(id) ON DELETE CASCADE,
    weekday           INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6), -- Monday = 0
    display_order     INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    kind              TEXT    NOT NULL CHECK(kind IN ('strength', 'sport', 'recovery')),
    intensity         TEXT    NOT NULL CHECK(intensity IN ('easy', 'moderate', 'hard', 'heavy')),
    estimated_minutes INTEGER,
    optional          INTEGER NOT NULL DEFAULT 0 CHECK(optional IN (0, 1)),
    notes             TEXT,
    UNIQUE(training_plan_id, display_order)
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_plan_weekday ON plan_workouts(training_plan_id, weekday);

CREATE TABLE IF NOT EXISTS plan_exercises (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_workout_id   INTEGER NOT NULL REFERENCES plan_workouts(id) ON DELETE CASCADE,
    exercise_id       INTEGER NOT NULL REFERENCES exercise_catalog(id),
    exercise_order    INTEGER NOT NULL,
    block_label       TEXT,
    sets_target       INTEGER NOT NULL CHECK(sets_target > 0),
    reps_min          INTEGER,
    reps_max          INTEGER,
    tempo             TEXT,
    rest_seconds      INTEGER,
    starting_weight   REAL,
    weight_unit       TEXT    NOT NULL DEFAULT 'bodyweight',
    substitution_note TEXT,
    notes             TEXT,
    UNIQUE(plan_workout_id, exercise_order)
);
CREATE INDEX IF NOT EXISTS idx_plan_exercises_workout ON plan_exercises(plan_workout_id);

-- Confirm-before-write manual logging (schema v9, D47). A pending draft is
-- not a workout. Its JSON payload is only materialized into the canonical
-- logbook after an explicit confirmation action.
CREATE TABLE IF NOT EXISTS manual_workout_drafts (
    id                 TEXT PRIMARY KEY,
    payload_json       TEXT    NOT NULL,
    status             TEXT    NOT NULL CHECK(status IN ('pending', 'confirmed', 'cancelled')),
    created_at         INTEGER NOT NULL,
    confirmed_at       INTEGER,
    workout_session_id INTEGER REFERENCES workout_sessions(id),
    CHECK(
        (status = 'confirmed' AND confirmed_at IS NOT NULL AND workout_session_id IS NOT NULL)
        OR (status != 'confirmed' AND confirmed_at IS NULL AND workout_session_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_manual_workout_drafts_status ON manual_workout_drafts(status);

-- Coaching proposals (schema v10, D48). Evidence and the exact proposed plan
-- diff are frozen before review. Acceptance creates a new plan version; it
-- never rewrites the plan that generated the proposal.
CREATE TABLE IF NOT EXISTS coaching_proposals (
    id                TEXT PRIMARY KEY,
    training_plan_id  INTEGER NOT NULL REFERENCES training_plans(id),
    kind              TEXT    NOT NULL,
    status            TEXT    NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected')),
    title             TEXT    NOT NULL,
    rationale         TEXT    NOT NULL,
    evidence_json     TEXT    NOT NULL,
    change_json       TEXT    NOT NULL,
    created_at        INTEGER NOT NULL,
    resolved_at       INTEGER,
    resulting_plan_id INTEGER REFERENCES training_plans(id),
    CHECK(
        (status = 'accepted' AND resolved_at IS NOT NULL AND resulting_plan_id IS NOT NULL)
        OR (status = 'rejected' AND resolved_at IS NOT NULL AND resulting_plan_id IS NULL)
        OR (status = 'pending' AND resolved_at IS NULL AND resulting_plan_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_coaching_proposals_status ON coaching_proposals(status);

-- Private monthly Excel report source evidence (schema v14, D82). Raw files
-- live outside SQLite under an owner-only directory. Rows are normalized by an
-- explicit versioned mapping, but never silently promoted into domain tables.
CREATE TABLE IF NOT EXISTS monthly_report_imports (
    id                     TEXT PRIMARY KEY,
    report_type            TEXT    NOT NULL,
    report_period          TEXT    NOT NULL,
    revision               INTEGER NOT NULL CHECK(revision > 0),
    status                 TEXT    NOT NULL CHECK(status IN ('active', 'superseded')),
    source_filename        TEXT    NOT NULL,
    stored_relpath         TEXT    NOT NULL UNIQUE,
    content_sha256         TEXT    NOT NULL,
    size_bytes             INTEGER NOT NULL CHECK(size_bytes >= 0),
    mapping_id             TEXT    NOT NULL,
    mapping_version        INTEGER NOT NULL CHECK(mapping_version > 0),
    mapping_hash           TEXT    NOT NULL,
    mapping_json           TEXT    NOT NULL,
    sheet_name             TEXT    NOT NULL,
    row_count              INTEGER NOT NULL CHECK(row_count >= 0),
    rows_digest            TEXT    NOT NULL,
    parser_version         INTEGER NOT NULL CHECK(parser_version > 0),
    supersedes_import_id   TEXT REFERENCES monthly_report_imports(id),
    imported_at            INTEGER NOT NULL,
    UNIQUE(report_type, report_period, revision),
    UNIQUE(report_type, report_period, content_sha256, mapping_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_report_active
    ON monthly_report_imports(report_type, report_period)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_monthly_report_period
    ON monthly_report_imports(report_period, status);
CREATE TRIGGER IF NOT EXISTS monthly_report_mapping_version_immutable
BEFORE INSERT ON monthly_report_imports
WHEN EXISTS (
    SELECT 1
    FROM monthly_report_imports
    WHERE mapping_id = NEW.mapping_id
      AND mapping_version = NEW.mapping_version
      AND mapping_hash <> NEW.mapping_hash
)
BEGIN
    SELECT RAISE(ABORT, 'mapping content changed without incrementing mapping_version');
END;

CREATE TABLE IF NOT EXISTS monthly_report_rows (
    import_id          TEXT    NOT NULL
        REFERENCES monthly_report_imports(id) ON DELETE CASCADE,
    source_row_number  INTEGER NOT NULL CHECK(source_row_number > 0),
    row_key            TEXT    NOT NULL,
    row_hash           TEXT    NOT NULL,
    data_json          TEXT    NOT NULL,
    PRIMARY KEY(import_id, source_row_number),
    UNIQUE(import_id, row_key)
);
CREATE INDEX IF NOT EXISTS idx_monthly_report_rows_key
    ON monthly_report_rows(row_key);
"""


def _migrate_metrics_daily_columns(conn: sqlite3.Connection) -> None:
    """Additive metrics_daily columns for DBs created before their schema version."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(metrics_daily)")]
    if "timezone" not in cols:  # D25, schema v2
        conn.execute("ALTER TABLE metrics_daily ADD COLUMN timezone TEXT")
    if "provisional" not in cols:  # D26/D30, schema v3
        conn.execute("ALTER TABLE metrics_daily ADD COLUMN provisional INTEGER")
    if "coverage_pct" not in cols:  # D26, schema v4
        conn.execute("ALTER TABLE metrics_daily ADD COLUMN coverage_pct REAL")
    if "time_in_zones_json" not in cols:  # D32, schema v5
        conn.execute("ALTER TABLE metrics_daily ADD COLUMN time_in_zones_json TEXT")


def _migrate_workout_set_columns(conn: sqlite3.Connection) -> None:
    """D40 follow-up: preserve Strong's non-numeric `Rest Timer` rows."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(workout_sets)")]
    if not cols:
        return
    if "sequence_order" not in cols:
        conn.execute("ALTER TABLE workout_sets ADD COLUMN sequence_order INTEGER")
    if "set_type" not in cols:
        conn.execute("ALTER TABLE workout_sets ADD COLUMN set_type TEXT NOT NULL DEFAULT 'work'")
    if "source_set_label" not in cols:
        conn.execute("ALTER TABLE workout_sets ADD COLUMN source_set_label TEXT")


def _migrate_rr_columns(conn: sqlite3.Connection) -> None:
    """D69: add a stable per-record RR position for historical deduplication."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(rr_intervals)")]
    if "rr_index" not in cols:
        conn.execute("ALTER TABLE rr_intervals ADD COLUMN rr_index INTEGER")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rr_history_dedup "
        "ON rr_intervals(device_ts_utc, source, rr_index) "
        "WHERE device_ts_utc IS NOT NULL AND rr_index IS NOT NULL"
    )


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    parent_existed = db_path.parent.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        secure_directory(db_path.parent)
    conn = sqlite3.connect(db_path)
    secure_sqlite_files(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    secure_sqlite_files(db_path)
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    _migrate_metrics_daily_columns(conn)
    _migrate_workout_set_columns(conn)
    _migrate_rr_columns(conn)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('dataset_id', ?)",
        (str(uuid.uuid4()),),
    )
    conn.commit()
    secure_sqlite_files(db_path)
    return conn
