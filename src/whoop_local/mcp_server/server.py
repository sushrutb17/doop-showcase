"""Local MCP server (Phase 3). Contract: MCP_SPEC.md. Read-only, stdio
transport, no auth (local single-user). The database must already exist and is
opened with SQLite ``mode=ro`` plus ``query_only``. The server never writes or
triggers captures — capture is a separate process (`capture/collector.py`).

Run directly for local testing:
    python -m whoop_local.mcp_server.server

Wire into Claude Code by adding it as an MCP server (project or user scope)
pointing at this module with the venv's Python interpreter.
"""

import argparse
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.mcp_server import queries

mcp = FastMCP("whoop-local")

_state: dict[str, object] = {"conn": None, "db_path": None}


def _conn() -> sqlite3.Connection:
    if _state["conn"] is None:
        raise RuntimeError("Server not initialized — call configure(db_path) first")
    return _state["conn"]  # type: ignore[return-value]


def configure(db_path: str) -> None:
    resolved = Path(db_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"MCP database must already exist: {resolved}. Initialize it with doop-init first."
        )

    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        conn.close()
        raise

    previous = _state.get("conn")
    if isinstance(previous, sqlite3.Connection):
        previous.close()
    _state["db_path"] = str(resolved)
    _state["conn"] = conn


@mcp.tool()
def get_status() -> dict:
    """Pipeline health, per-host sync freshness, and coverage gaps."""
    return queries.get_status(_conn(), str(_state["db_path"]))


@mcp.tool()
def get_latest_recovery() -> dict:
    """Most recent day's recovery proxy, HRV, resting HR, and sleep minutes."""
    return queries.get_latest_recovery(_conn())


@mcp.tool()
def get_hrv(start_date: str, end_date: str) -> dict:
    """Daily HRV (RMSSD/SDNN) series over an inclusive date range (YYYY-MM-DD)."""
    return queries.get_hrv(_conn(), start_date, end_date)


@mcp.tool()
def get_sleep(date: str) -> dict:
    """Detected sleep minutes and segments for a given date (YYYY-MM-DD),
    plus Apple-export sleep for the same date when imported (labeled, not blended)."""
    return queries.get_sleep(_conn(), date)


@mcp.tool()
def get_strain(date: str) -> dict:
    """TRIMP-style strain proxy + time-in-zones for a given date (YYYY-MM-DD),
    plus Apple-export workouts for the same date when imported (context, not an input)."""
    return queries.get_strain(_conn(), date)


@mcp.tool()
def query_readings(metric: str, start_utc: int, end_utc: int, limit: int = 5000) -> dict:
    """Raw time-series readings. metric in {hr, rr, spo2, temp, hr_apple};
    start/end are epoch ms. hr_apple is Apple-export HR for WHOOP-vs-Watch comparison."""
    return queries.query_readings(_conn(), metric, start_utc, end_utc, limit)


@mcp.tool()
def get_workout_history(start_date: str, end_date: str, limit: int = 50) -> dict:
    """Detailed-logbook workout sessions over an inclusive local-date range."""
    return queries.get_workout_history(_conn(), start_date, end_date, limit)


@mcp.tool()
def get_exercise_history(exercise_name: str, limit: int = 20) -> dict:
    """Recent sets and progress statistics for an exact canonical exercise name."""
    return queries.get_exercise_history(_conn(), exercise_name, limit)


@mcp.tool()
def search_exercises(query: str = "", limit: int = 20) -> dict:
    """Discover canonical exercise names and their session counts."""
    return queries.search_exercises(_conn(), query, limit)


@mcp.tool()
def search_exercise_library(
    query: str = "",
    equipment: str | None = None,
    target: str | None = None,
    body_part: str | None = None,
    review_status: str = "all",
    limit: int = 20,
) -> dict:
    """Search prospective exercise metadata. Results are not performed/planned/canonical by default."""
    return queries.search_exercise_library(
        _conn(), query, equipment, target, body_part, review_status, limit
    )


@mcp.tool()
def get_exercise_library_entry(external_id: str) -> dict:
    """Get one prospective exercise's metadata, instructions, provenance, and review state."""
    return queries.get_exercise_library_entry(_conn(), external_id)


@mcp.tool()
def get_training_summary(start_date: str, end_date: str) -> dict:
    """Workout frequency, work sets, reps, volume, and top exercises for a range."""
    return queries.get_training_summary(_conn(), start_date, end_date)


@mcp.tool()
def get_active_plan() -> dict:
    """Current intended weekly plan and exercise prescriptions; never records completion."""
    return queries.get_active_plan(_conn())


@mcp.tool()
def get_coaching_proposals(status: str = "pending", limit: int = 20) -> dict:
    """Review coaching proposals and their evidence; this MCP tool cannot resolve them."""
    return queries.get_coaching_proposals(_conn(), status, limit)


@mcp.tool()
def get_plan_adherence(start_date: str, end_date: str) -> dict:
    """Required/optional plan slots matched to detailed strength and Apple Health sport records."""
    return queries.get_plan_adherence(_conn(), start_date, end_date)


@mcp.tool()
def get_training_guidance(date: str) -> dict:
    """Exact planned prescriptions plus confidence-gated advisory guidance and safe presentation rules."""
    return queries.get_training_guidance(_conn(), date)


@mcp.tool()
def get_telegram_training_guidance(date: str) -> dict:
    """Minimum-disclosure preformatted today/pre-workout answer; relay text verbatim."""
    return queries.get_telegram_training_guidance(_conn(), date)


@mcp.tool()
def get_weekly_training_review(week_start: str) -> dict:
    """Seven-day plan adherence, completed workload, and actionable recovery review; week starts Monday."""
    return queries.get_weekly_training_review(_conn(), week_start)


@mcp.tool()
def get_telegram_weekly_review(week_start: str) -> dict:
    """Minimum-disclosure preformatted weekly answer; relay text verbatim."""
    return queries.get_telegram_weekly_review(_conn(), week_start)


@mcp.resource("whoop://daily-summary")
def daily_summary() -> str:
    """Compact markdown summary of the latest day: recovery + sleep + freshness."""
    conn = _conn()
    recovery = queries.get_latest_recovery(conn)
    status = queries.get_status(conn, str(_state["db_path"]))

    if recovery.get("status") == "no_data":
        return "# WHOOP Daily Summary\n\nNo data yet."

    sleep = queries.get_sleep(conn, recovery["date"])
    confidence = recovery.get("confidence") or {}
    if confidence.get("actionable"):
        recovery_line = (
            f"- Recovery proxy: {recovery['recovery_proxy']} ({confidence.get('level')} confidence)"
        )
    else:
        reasons = ", ".join(confidence.get("reasons", [])) or "insufficient inputs"
        recovery_line = f"- Recovery proxy: withheld ({reasons})"

    sleep_label = "Sleep"
    coverage_pct = sleep.get("coverage_pct")
    if coverage_pct is not None and coverage_pct < 0.95:
        sleep_label = "Sleep (at least"
        sleep_line = f"- {sleep_label} {sleep.get('sleep_minutes')} minutes, capture was only {coverage_pct:.0%} complete)"
    else:
        sleep_line = f"- Sleep: {sleep.get('sleep_minutes')} minutes"

    lines = [
        f"# WHOOP Daily Summary — {recovery['date']}",
        "",
        recovery_line,
        f"- RMSSD: {recovery['rmssd_ms']} ms (baseline: {recovery['hrv_baseline_ms']} ms)",
        f"- Resting HR: {recovery['rhr_bpm']} bpm",
        sleep_line,
        f"- Last reading: {status['last_reading_utc']}",
        "",
        f"_{queries.RECOVERY_NOTE}_",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="validate the configured database read-only without printing its contents, then exit",
    )
    args = parser.parse_args()

    if args.healthcheck:
        from whoop_local.integrations.openclaw_healthcheck import check_readiness

        return 0 if not check_readiness(args.db) else 1

    configure(args.db)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
