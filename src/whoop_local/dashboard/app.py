"""Training OS dashboard with confidence-gated guidance and confirmed writes.

The presentation is coach-first, but Doop's query and confirmation contracts
remain authoritative.  No dashboard copy may turn a plan into a completion or
make a non-actionable biometric value influence training.

Run with:
    uv run streamlit run src/whoop_local/dashboard/app.py --server.address 127.0.0.1

The database and timezone come from the local runtime configuration described
in ``USING_THIS_CODE.md``.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta
from html import escape

import numpy as np
import pandas as pd
import streamlit as st

from whoop_local.coaching import service as coaching_service
from whoop_local.config import DEFAULT_DB_PATH, local_timezone
from whoop_local.dashboard.training_os import (
    build_decision,
    build_week_rows,
    rep_range,
    system_freshness,
)
from whoop_local.dashboard.workout_entry import plan_log_rows, rows_to_payload
from whoop_local.db.schema import init_db
from whoop_local.manual_logging import service as logging_service
from whoop_local.mcp_server import queries


def get_connection() -> sqlite3.Connection:
    """Use a fresh configured connection because the collector writes continuously."""
    conn = init_db(DEFAULT_DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _e(value: object) -> str:
    return escape("—" if value is None else str(value))


def _inject_theme() -> None:
    st.html(
        """
        <style>
        :root {
          --os-ink: #0b1020;
          --os-muted: #667085;
          --os-line: #d8d9dc;
          --os-paper: #f7f6f2;
          --os-white: #fffefa;
          --os-orange: #ff4b0a;
          --os-blue: #1558a6;
          --os-green: #238b57;
          --os-amber: #a85d12;
        }
        html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
          background: var(--os-paper);
          color: var(--os-ink);
        }
        [data-testid="stHeader"], #MainMenu, footer, [data-testid="stToolbar"] {
          display: none !important;
        }
        .block-container {
          max-width: 1488px;
          padding: 1.35rem 2rem 4rem;
        }
        p, label, [data-testid="stMarkdownContainer"] {
          font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .os-header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 2rem;
          padding: .2rem 0 1rem;
          border-bottom: 1px solid var(--os-line);
        }
        .os-wordmark {
          color: var(--os-ink);
          font: 800 2.15rem/1 Inter, ui-sans-serif, sans-serif;
          letter-spacing: -.055em;
        }
        .os-tagline, .os-date, .os-status-label, .os-note {
          color: var(--os-muted);
          font: 500 .82rem/1.45 Inter, ui-sans-serif, sans-serif;
        }
        .os-tagline { margin-top: .45rem; }
        .os-header-meta {
          display: flex;
          align-items: center;
          gap: 1.4rem;
          text-align: left;
        }
        .os-header-meta > div + div {
          border-left: 1px solid var(--os-line);
          padding-left: 1.4rem;
        }
        .os-status-title {
          color: var(--os-ink);
          font: 700 .75rem/1 Inter, ui-sans-serif, sans-serif;
          letter-spacing: .06em;
          text-transform: uppercase;
          margin-bottom: .42rem;
        }
        .os-status-dot {
          display: inline-block;
          width: .52rem;
          height: .52rem;
          border-radius: 50%;
          margin-right: .42rem;
          background: var(--os-green);
        }
        .os-status-dot.attention { background: var(--os-orange); }
        [data-testid="stButtonGroup"] {
          margin: .45rem 0 .8rem;
        }
        [data-testid="stButtonGroup"] button {
          border-radius: 0 !important;
          border-color: transparent !important;
          background: transparent !important;
          color: var(--os-muted) !important;
          font-size: .78rem !important;
          font-weight: 700 !important;
          letter-spacing: .055em !important;
          text-transform: uppercase !important;
          min-height: 2.5rem !important;
        }
        [data-testid="stButtonGroup"] button[aria-pressed="true"] {
          color: var(--os-ink) !important;
          border-bottom: 2px solid var(--os-orange) !important;
        }
        .os-kicker, .os-section-title, .os-fact-label, .os-signal-label,
        .os-week-day, .os-table-head, .os-why-label {
          color: var(--os-ink);
          font: 800 .72rem/1.2 Inter, ui-sans-serif, sans-serif;
          letter-spacing: .07em;
          text-transform: uppercase;
        }
        .os-decision {
          color: var(--os-ink);
          font-family: Impact, Haettenschweiler, "Arial Narrow Bold", sans-serif;
          font-size: clamp(2.65rem, 3.8vw, 4rem);
          font-weight: 900;
          letter-spacing: -.025em;
          line-height: .95;
          margin: .72rem 0 1rem;
          max-width: 1000px;
        }
        .os-evidence {
          display: flex;
          flex-wrap: wrap;
          gap: .6rem 1rem;
          color: #293244;
          font: 650 .9rem/1.3 Inter, ui-sans-serif, sans-serif;
          margin-bottom: 1.4rem;
        }
        .os-evidence span + span {
          border-left: 1px solid var(--os-line);
          padding-left: 1rem;
        }
        .os-facts {
          display: grid;
          grid-template-columns: 1.25fr .8fr .8fr 1.45fr .8fr 1.4fr;
          border-top: 1px solid var(--os-line);
          border-bottom: 1px solid var(--os-line);
        }
        .os-fact {
          padding: 1rem 1rem 1rem 0;
          min-width: 0;
        }
        .os-fact + .os-fact {
          border-left: 1px solid var(--os-line);
          padding-left: 1rem;
        }
        .os-fact-value {
          color: var(--os-ink);
          font: 500 1rem/1.25 Inter, ui-sans-serif, sans-serif;
          margin-top: .5rem;
          overflow-wrap: anywhere;
        }
        .os-coach-voice {
          color: #4a5262;
          font: italic 500 .78rem/1.45 Inter, ui-sans-serif, sans-serif;
          margin-top: .7rem;
        }
        .os-cta-spacer { height: 2.15rem; }
        div[data-testid="stButton"] > button[kind="primary"] {
          background: var(--os-orange) !important;
          border: 1px solid var(--os-orange) !important;
          border-radius: 0 !important;
          color: white !important;
          font-weight: 800 !important;
          letter-spacing: .035em !important;
          min-height: 3.8rem;
          text-transform: uppercase;
        }
        div[data-testid="stButton"] > button[kind="primary"]:hover {
          background: #de3d00 !important;
          border-color: #de3d00 !important;
        }
        div[data-testid="stButton"] > button:focus-visible,
        [data-testid="stButtonGroup"] button:focus-visible {
          outline: 3px solid #1b63b8 !important;
          outline-offset: 2px;
        }
        .os-band {
          border-top: 1px solid var(--os-line);
          border-bottom: 1px solid var(--os-line);
          margin-top: 1.25rem;
          padding: 1.05rem 0;
        }
        .os-section-heading {
          display: flex;
          align-items: baseline;
          gap: .8rem;
          margin-bottom: .75rem;
        }
        .os-section-context {
          color: var(--os-muted);
          font: 500 .75rem/1 Inter, ui-sans-serif, sans-serif;
        }
        .os-week-grid {
          display: grid;
          grid-template-columns: repeat(7, minmax(0, 1fr));
          border-top: 1px solid var(--os-line);
          border-bottom: 1px solid var(--os-line);
        }
        .os-week-cell {
          min-height: 8.7rem;
          padding: .85rem .65rem;
          text-align: center;
          position: relative;
        }
        .os-week-cell + .os-week-cell { border-left: 1px solid var(--os-line); }
        .os-week-cell.today {
          background: #fff4ed;
          box-shadow: inset 0 -3px 0 var(--os-orange);
        }
        .os-week-name {
          color: var(--os-ink);
          font: 650 .77rem/1.25 Inter, ui-sans-serif, sans-serif;
          margin-top: .9rem;
          min-height: 2.15rem;
        }
        .os-week-meta, .os-week-state {
          color: var(--os-muted);
          font: 500 .68rem/1.25 Inter, ui-sans-serif, sans-serif;
          margin-top: .65rem;
        }
        .os-week-state { color: var(--os-blue); font-weight: 700; }
        .os-week-state.missing { color: var(--os-amber); }
        .os-signals {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          border-top: 1px solid var(--os-line);
          border-bottom: 1px solid var(--os-line);
        }
        .os-signal { padding: 1rem .65rem 1.1rem 0; }
        .os-signal + .os-signal {
          border-left: 1px solid var(--os-line);
          padding-left: .65rem;
        }
        .os-signal-value {
          color: var(--os-ink);
          font: 500 1.8rem/1 Inter, ui-sans-serif, sans-serif;
          letter-spacing: -.04em;
          margin-top: .6rem;
        }
        .os-signal-unit {
          color: var(--os-muted);
          font: 500 .68rem/1 Inter, ui-sans-serif, sans-serif;
          margin-top: .35rem;
        }
        .os-lower { margin-top: 1.1rem; }
        .os-why-row {
          border-top: 1px solid var(--os-line);
          padding: .65rem 0;
        }
        .os-why-copy {
          color: #3e4758;
          font: 500 .78rem/1.45 Inter, ui-sans-serif, sans-serif;
          margin-top: .32rem;
        }
        .os-session-table { border-top: 1px solid var(--os-line); }
        .os-session-row {
          display: grid;
          grid-template-columns: 2.1rem 2.1fr 1.2fr .9fr .75fr;
          align-items: center;
          gap: .6rem;
          padding: .55rem 0;
          border-bottom: 1px solid var(--os-line);
          color: var(--os-ink);
          font: 500 .78rem/1.25 Inter, ui-sans-serif, sans-serif;
        }
        .os-session-row.head {
          color: var(--os-muted);
          font-size: .65rem;
          font-weight: 800;
          letter-spacing: .05em;
          text-transform: uppercase;
        }
        .os-disclaimer {
          color: var(--os-muted);
          font: 500 .71rem/1.45 Inter, ui-sans-serif, sans-serif;
          margin: 1.25rem 0 0;
        }
        .os-page-title {
          font: 800 2.1rem/1.05 Inter, ui-sans-serif, sans-serif;
          letter-spacing: -.04em;
          margin: .4rem 0 .45rem;
        }
        .os-page-subtitle {
          color: var(--os-muted);
          font: 500 .88rem/1.5 Inter, ui-sans-serif, sans-serif;
          margin-bottom: 1.2rem;
        }
        .os-footer {
          border-top: 1px solid var(--os-line);
          margin-top: 2.3rem;
          padding-top: 1rem;
          display: flex;
          justify-content: space-between;
          gap: 1rem;
          color: var(--os-muted);
          font: 500 .68rem/1.4 Inter, ui-sans-serif, sans-serif;
        }
        [data-testid="stMetric"] {
          background: transparent;
          border-left: 2px solid var(--os-blue);
          padding-left: .75rem;
        }
        [data-testid="stMetricLabel"] { color: var(--os-muted); }
        [data-testid="stMetricValue"] { color: var(--os-ink); }
        [data-testid="stDataFrame"], [data-testid="stTable"] {
          border: 1px solid var(--os-line);
        }
        [data-testid="stExpander"] {
          border: 1px solid var(--os-line);
          border-radius: 0;
          background: var(--os-white);
        }
        [data-testid="stAlert"] { border-radius: 0; }
        @media (max-width: 900px) {
          .block-container { padding: 1rem; }
          .os-header, .os-header-meta { flex-direction: column; }
          .os-header-meta > div + div { border-left: 0; padding-left: 0; }
          .os-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .os-fact { border-bottom: 1px solid var(--os-line); }
          .os-week-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .os-week-cell + .os-week-cell { border-left: 0; }
          .os-session-row { grid-template-columns: 1.6rem 1.7fr 1fr; }
          .os-session-row > :nth-child(n+4) { display: none; }
        }
        </style>
        """
    )


def _render_header(today: date, freshness: dict) -> str:
    pretty_date = f"{today.strftime('%A, %B')} {today.day}, {today.year}"
    st.html(
        f"""
        <header class="os-header">
          <div>
            <div class="os-wordmark">Doop Training</div>
            <div class="os-tagline">Evidence-based training. Your context. Smarter decisions.</div>
          </div>
          <div class="os-header-meta">
            <div>
              <div class="os-date">{_e(pretty_date)}</div>
              <div class="os-date">Local time</div>
            </div>
            <div>
              <div class="os-status-title">System status</div>
              <div class="os-status-label">
                <span class="os-status-dot {_e(freshness["tone"])}"></span>{_e(freshness["label"])}
              </div>
            </div>
          </div>
        </header>
        """
    )
    return (
        st.segmented_control(
            "Workspace",
            ["Today", "Plan & log", "Progress", "Health", "System"],
            default="Today",
            key="training_os_workspace",
            label_visibility="collapsed",
            width="stretch",
        )
        or "Today"
    )


def _render_hero(decision: dict) -> bool:
    primary = decision.get("primary") or {}
    recovery_evidence = (
        f"Recovery evidence actionable (proxy {decision['recovery']})"
        if "Actionable" in decision["confidence"] and decision["recovery"] != "Withheld"
        else "Biometrics are not changing the written plan"
    )
    evidence = [recovery_evidence]
    if primary.get("kind") == "strength":
        evidence.extend(("Keep 2–3 reps in reserve", "Use controlled technique"))
    evidence_html = "".join(f"<span>{_e(item)}</span>" for item in evidence)
    duration = f"{decision['duration']} min" if decision.get("duration") else "—"

    decision_col, action_col = st.columns([7.4, 1.7], gap="large")
    with decision_col:
        st.html(
            f"""
            <section>
              <div class="os-kicker">BROC's decision</div>
              <div class="os-decision">{_e(decision["headline"])}</div>
              <div class="os-evidence">{evidence_html}</div>
              <div class="os-facts">
                <div class="os-fact"><div class="os-fact-label">Session</div><div class="os-fact-value">{_e(decision["session"])}</div></div>
                <div class="os-fact"><div class="os-fact-label">Intensity</div><div class="os-fact-value">{_e(decision["intensity"])}</div></div>
                <div class="os-fact"><div class="os-fact-label">Duration</div><div class="os-fact-value">{_e(duration)}</div></div>
                <div class="os-fact"><div class="os-fact-label">Status</div><div class="os-fact-value">{_e(decision["status"])}</div></div>
                <div class="os-fact"><div class="os-fact-label">Recovery proxy</div><div class="os-fact-value">{_e(decision["recovery"])}</div></div>
                <div class="os-fact"><div class="os-fact-label">Confidence / actionable</div><div class="os-fact-value">{_e(decision["confidence"])}</div></div>
              </div>
              <div class="os-coach-voice">“{_e(decision["quote"])}” — BROC</div>
            </section>
            """
        )
    opened = False
    with action_col:
        st.html('<div class="os-cta-spacer"></div>')
        if decision.get("cta"):
            opened = st.button(
                decision["cta"],
                type="primary",
                icon=":material/arrow_forward:",
                icon_position="right",
                width="stretch",
                key="open_today_session",
            )
        st.html(
            '<div class="os-note" style="margin-top:.8rem">Nothing is logged until you review and confirm it.</div>'
        )
    return opened


def _week_cell(row: dict) -> str:
    minutes = f"{row['minutes']} min" if row.get("minutes") else "—"
    state_class = "missing" if row["state"] == "No matching log" else ""
    today_class = "today" if row["is_today"] else ""
    return f"""
      <div class="os-week-cell {today_class}">
        <div class="os-week-day">{_e(row["day"])} {row["date_number"]}</div>
        <div class="os-week-name">{_e(row["name"])}</div>
        <div class="os-week-meta">{_e(row["intensity"])} · {_e(minutes)}</div>
        <div class="os-week-state {state_class}">{_e(row["state"])}</div>
      </div>
    """


def _render_week_and_signals(
    active_plan: dict,
    weekly: dict,
    today: date,
    recovery: dict,
    sleep: dict,
) -> None:
    week_rows = build_week_rows(active_plan, weekly, today)
    week_start = week_rows[0]["date"]
    week_end = week_rows[-1]["date"]
    week_context = f"{week_start.strftime('%b')} {week_start.day} – {week_end.strftime('%b')} {week_end.day}, {week_end.year}"
    week_html = "".join(_week_cell(row) for row in week_rows)
    recovery_value = recovery.get("recovery_proxy")
    signals = [
        ("Sleep", sleep.get("sleep_minutes"), "min"),
        (
            "RMSSD",
            None if recovery.get("rmssd_ms") is None else f"{recovery['rmssd_ms']:.1f}",
            "ms",
        ),
        (
            "Resting HR",
            None if recovery.get("rhr_bpm") is None else f"{recovery['rhr_bpm']:.0f}",
            "bpm",
        ),
        ("Recovery proxy", None if recovery_value is None else f"{recovery_value:.0f}", "/100"),
    ]
    signal_html = "".join(
        f"""
        <div class="os-signal">
          <div class="os-signal-label">{_e(label)}</div>
          <div class="os-signal-value">{_e(value)}</div>
          <div class="os-signal-unit">{_e(unit)}</div>
        </div>
        """
        for label, value, unit in signals
    )

    week_col, signals_col = st.columns([2.15, 1], gap="large")
    with week_col:
        st.html(
            f"""
            <section class="os-band">
              <div class="os-section-heading">
                <div class="os-section-title">Your week at a glance</div>
                <div class="os-section-context">{_e(week_context)}</div>
              </div>
              <div class="os-week-grid">{week_html}</div>
            </section>
            """
        )
    with signals_col:
        st.html(
            f"""
            <section class="os-band">
              <div class="os-section-heading"><div class="os-section-title">Key signals today</div></div>
              <div class="os-signals">{signal_html}</div>
              <div class="os-note" style="margin-top:.65rem">Recovery is shown only when the D39 confidence gate passes.</div>
            </section>
            """
        )


def _rest_label(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} sec"


def _render_why_and_outline(decision: dict, active_plan: dict) -> None:
    primary = decision.get("primary")
    recommendation = decision["evidence"][0]
    plan_notes = (active_plan.get("plan") or {}).get("notes") or "Follow the written plan."
    joint_note = (
        primary.get("workout_notes")
        if primary
        else "Pain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides this guidance."
    )
    why_rows = [
        ("Evidence first", recommendation),
        ("Quality focus", plan_notes),
        ("Pain-aware plan", joint_note),
    ]
    why_html = "".join(
        f'<div class="os-why-row"><div class="os-why-label">{_e(title)}</div><div class="os-why-copy">{_e(copy)}</div></div>'
        for title, copy in why_rows
    )

    if primary:
        rows = []
        for index, exercise in enumerate(primary.get("exercises", [])[:4], start=1):
            weight = exercise.get("starting_weight")
            prescription = rep_range(exercise)
            if weight is not None:
                prescription += f" @ {weight:g} {exercise.get('weight_unit', '')}"
            rows.append(
                f"""
                <div class="os-session-row">
                  <div>{index}</div><div>{_e(exercise["name"])}</div><div>{_e(prescription)}</div>
                  <div>{_e(primary.get("intensity"))}</div><div>{_e(_rest_label(exercise.get("rest_seconds")))}</div>
                </div>
                """
            )
        outline_html = "".join(rows)
    else:
        outline_html = (
            '<div class="os-why-copy" style="padding:1rem 0">No session is planned today.</div>'
        )

    why_col, outline_col = st.columns([1.15, 1.85], gap="large")
    with why_col:
        st.html(
            f'<section class="os-lower"><div class="os-section-heading"><div class="os-section-title">Why this call</div></div>{why_html}</section>'
        )
    with outline_col:
        duration = f"Est. {decision['duration']} min" if decision.get("duration") else ""
        st.html(
            f"""
            <section class="os-lower">
              <div class="os-section-heading"><div class="os-section-title">Today's session outline</div><div class="os-section-context">{_e(duration)}</div></div>
              <div class="os-session-table">
                <div class="os-session-row head"><div>#</div><div>Exercise</div><div>Sets × reps</div><div>Intensity</div><div>Rest</div></div>
                {outline_html}
              </div>
            </section>
            """
        )


def _render_workout_entry(
    conn: sqlite3.Connection,
    active_plan: dict,
    initial_workout: str | None = None,
    *,
    expanded: bool = True,
) -> None:
    strength_workouts = [
        workout for workout in active_plan.get("workouts", []) if workout["kind"] == "strength"
    ]
    if not strength_workouts:
        st.info("No planned strength workout is available.")
        return

    with st.expander("Review and log a planned strength workout", expanded=expanded):
        workout_names = [workout["name"] for workout in strength_workouts]
        initial_index = (
            workout_names.index(initial_workout) if initial_workout in workout_names else 0
        )
        selected_workout = st.selectbox(
            "Planned workout", workout_names, index=initial_index, key="log_workout_name"
        )
        workout = next(item for item in strength_workouts if item["name"] == selected_workout)
        now = datetime.now(local_timezone())
        log_col1, log_col2, log_col3 = st.columns(3)
        workout_date = log_col1.date_input("Workout date", value=now.date())
        workout_time = log_col2.time_input(
            "Start time", value=now.replace(second=0, microsecond=0).time()
        )
        duration = log_col3.number_input(
            "Duration (minutes)",
            min_value=1,
            max_value=240,
            value=workout.get("estimated_minutes") or 60,
        )
        entry_rows = pd.DataFrame(plan_log_rows(active_plan, selected_workout))
        edited = st.data_editor(
            entry_rows,
            width="stretch",
            hide_index=True,
            key=f"entry_{active_plan['plan']['version']}_{selected_workout}",
            disabled=["Exercise", "Set", "Unit"],
            column_config={
                "Include": st.column_config.CheckboxColumn("Completed"),
                "Weight": st.column_config.NumberColumn("Weight", min_value=0.0, step=0.5),
                "Unit": st.column_config.TextColumn(
                    "Unit", help="Derived from the active plan prescription"
                ),
                "Reps": st.column_config.NumberColumn("Reps", min_value=1, step=1),
                "RIR": st.column_config.NumberColumn("RIR", min_value=0, max_value=10, step=1),
                "RPE": st.column_config.NumberColumn("RPE", min_value=1, max_value=10, step=0.5),
            },
        )
        workout_notes = st.text_area("Workout notes", key="workout_notes")
        if st.button("Prepare workout for review", type="primary", key="prepare_workout"):
            try:
                payload = rows_to_payload(
                    edited.to_dict("records"),
                    selected_workout,
                    workout_date,
                    workout_time,
                    int(duration),
                    local_timezone(),
                    workout_notes,
                )
                draft = logging_service.prepare_draft(conn, payload)
                st.session_state["manual_draft_id"] = draft["draft_id"]
                st.success(
                    "Draft prepared. Nothing has been logged yet; review the exact workout below."
                )
            except ValueError as exc:
                st.error(str(exc))

        draft_id = st.session_state.get("manual_draft_id")
        if draft_id:
            try:
                draft = logging_service.get_draft(conn, draft_id)
            except ValueError:
                st.session_state.pop("manual_draft_id", None)
            else:
                if draft["status"] == "pending":
                    st.markdown("#### Pending workout review")
                    review_rows = [
                        {"Exercise": exercise["name"], "Sets": len(exercise["sets"])}
                        for exercise in draft["workout"]["exercises"]
                    ]
                    st.write(f"**{draft['workout']['name']}** · {draft['workout']['local_date']}")
                    st.dataframe(pd.DataFrame(review_rows), width="stretch", hide_index=True)
                    confirm_log = st.checkbox(
                        "I reviewed this workout and want to add it to my permanent logbook.",
                        key=f"confirm_log_{draft_id}",
                    )
                    confirm_col, cancel_col = st.columns(2)
                    if confirm_col.button(
                        "Confirm and log workout",
                        disabled=not confirm_log,
                        key=f"confirm_workout_{draft_id}",
                    ):
                        logging_service.confirm_draft(conn, draft_id)
                        st.session_state.pop("manual_draft_id", None)
                        st.success("Workout logged.")
                        st.rerun()
                    if cancel_col.button("Cancel draft", key=f"cancel_workout_{draft_id}"):
                        logging_service.cancel_draft(conn, draft_id)
                        st.session_state.pop("manual_draft_id", None)
                        st.rerun()


def _render_proposals(conn: sqlite3.Connection, active_plan: dict) -> None:
    strength_workouts = [
        workout for workout in active_plan.get("workouts", []) if workout["kind"] == "strength"
    ]
    st.markdown("#### Coaching proposals")
    planned_exercises = [
        (workout["name"], exercise["name"])
        for workout in strength_workouts
        for exercise in workout["exercises"]
    ]
    if not planned_exercises:
        st.caption("No planned exercises are available for evaluation.")
        return
    labels = [
        f"{workout_name} · {exercise_name}" for workout_name, exercise_name in planned_exercises
    ]
    selected_label = st.selectbox("Evaluate progression", labels, key="proposal_exercise")
    if st.button("Evaluate completed sessions", key="evaluate_sessions"):
        workout_name, exercise_name = planned_exercises[labels.index(selected_label)]
        try:
            proposal = coaching_service.propose_weight_progression(
                conn, workout_name, exercise_name
            )
            st.success(f"Proposal ready: {proposal['title']}")
            st.rerun()
        except ValueError as exc:
            st.info(str(exc))

    pending = queries.get_coaching_proposals(conn, "pending", limit=20)
    if not pending["proposals"]:
        st.caption(
            "No proposals awaiting review. Two qualifying logged sessions are required for progression."
        )
    for proposal in pending["proposals"]:
        with st.expander(proposal["title"], expanded=True):
            st.write(proposal["rationale"])
            evidence_sessions = proposal["evidence"].get("sessions") or []
            if evidence_sessions:
                st.dataframe(pd.DataFrame(evidence_sessions), width="stretch", hide_index=True)
            st.json(proposal["change"])
            accept_check = st.checkbox(
                "I reviewed the evidence and want to create a new plan version.",
                key=f"accept_proposal_{proposal['id']}",
            )
            accept_col, reject_col = st.columns(2)
            if accept_col.button(
                "Accept proposal", key=f"accept_{proposal['id']}", disabled=not accept_check
            ):
                coaching_service.accept_proposal(conn, proposal["id"])
                st.success("Proposal accepted; a new plan version is active.")
                st.rerun()
            if reject_col.button("Reject proposal", key=f"reject_{proposal['id']}"):
                coaching_service.reject_proposal(conn, proposal["id"])
                st.rerun()


def _render_today(
    conn: sqlite3.Connection,
    guidance: dict,
    active_plan: dict,
    weekly: dict,
    today: date,
    recovery: dict,
    sleep: dict,
) -> None:
    decision = build_decision(guidance)
    if _render_hero(decision):
        st.session_state["show_today_session"] = True
    _render_week_and_signals(active_plan, weekly, today, recovery, sleep)
    _render_why_and_outline(decision, active_plan)
    st.html(
        '<div class="os-disclaimer">Guidance is advisory and never edits the active plan. Pain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides it.</div>'
    )
    if st.session_state.get("show_today_session") and decision.get("primary"):
        st.html(
            '<div class="os-page-title" style="margin-top:2rem">Today’s session</div><div class="os-page-subtitle">Edit what actually happened, prepare a draft, then confirm it separately.</div>'
        )
        _render_workout_entry(
            conn, active_plan, initial_workout=decision["primary"]["name"], expanded=True
        )


def _render_plan_and_log(
    conn: sqlite3.Connection,
    active_plan: dict,
    weekly: dict,
    today_workout: str | None,
) -> None:
    st.html(
        '<div class="os-page-title">Plan & log</div><div class="os-page-subtitle">The versioned plan is intended work. The logbook changes only after a reviewed confirmation.</div>'
    )
    if active_plan.get("status") == "no_data":
        st.info("No active plan is available.")
        return
    schedule_rows = [
        {
            "Day": workout["day"],
            "Workout": workout["name"],
            "Type": workout["kind"],
            "Intensity": workout["intensity"],
            "Minutes": workout["estimated_minutes"],
            "Optional": workout["optional"],
        }
        for workout in active_plan["workouts"]
    ]
    st.dataframe(pd.DataFrame(schedule_rows), width="stretch", hide_index=True)
    adherence = weekly.get("adherence") or {}
    a1, a2, a3 = st.columns(3)
    a1.metric(
        "Matching completion records",
        f"{adherence.get('completed_required_slots', 0)}/{adherence.get('required_slots', 0)}",
    )
    a2.metric("Detailed work sets", (weekly.get("training") or {}).get("work_sets", 0))
    average = weekly.get("average_actionable_recovery")
    a3.metric("Avg actionable recovery", f"{average:.0f}" if average is not None else "—")
    st.caption((weekly.get("summary") or "") + " " + (weekly.get("note") or ""))
    _render_workout_entry(conn, active_plan, initial_workout=today_workout, expanded=True)
    _render_proposals(conn, active_plan)


def _render_progress(
    conn: sqlite3.Connection,
    first_workout_date: str | None,
    last_workout_date: str | None,
    workout_count: int,
) -> None:
    st.html(
        '<div class="os-page-title">Progress</div><div class="os-page-subtitle">Canonical detailed history, exercise trends, and evidence for future progression.</div>'
    )
    if not first_workout_date or not last_workout_date:
        st.info("No detailed workouts imported yet.")
        return
    total_exercises = conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0]
    total_work_sets = conn.execute(
        "SELECT COUNT(*) FROM workout_sets WHERE set_type = 'work'"
    ).fetchone()[0]
    total_rest_events = conn.execute(
        "SELECT COUNT(*) FROM workout_sets WHERE set_type = 'rest'"
    ).fetchone()[0]
    w1, w2, w3, w4 = st.columns(4)
    w1.metric("Sessions", workout_count)
    w2.metric("Exercises", total_exercises)
    w3.metric("Work sets", total_work_sets)
    w4.metric("Rest events", total_rest_events)
    st.caption(
        f"Detailed Strong history: {first_workout_date} to {last_workout_date}. "
        "Newer Apple workout envelopes may exist, but they do not contain exercise/set detail."
    )
    recent = queries.get_workout_history(conn, first_workout_date, last_workout_date, limit=10)
    recent_df = pd.DataFrame(recent["workouts"])
    if not recent_df.empty:
        st.markdown("#### Recent detailed workouts")
        st.dataframe(
            recent_df[
                ["date", "name", "duration_min", "exercise_count", "work_sets", "rest_events"]
            ],
            width="stretch",
            hide_index=True,
        )

    exercise_names = [
        row[0]
        for row in conn.execute(
            "SELECT canonical_name FROM exercise_catalog ORDER BY canonical_name"
        )
    ]
    default_exercise = "Bench Press (Barbell)"
    default_index = (
        exercise_names.index(default_exercise) if default_exercise in exercise_names else 0
    )
    selected_exercise = st.selectbox("Exercise progress", exercise_names, index=default_index)
    history = queries.get_exercise_history(conn, selected_exercise, limit=12)
    summary = history.get("summary") or {}
    e1, e2, e3 = st.columns(3)
    e1.metric("Sessions returned", history.get("session_count", 0))
    max_weight = summary.get("max_weight")
    unit_label = ", ".join(summary.get("weight_units") or [])
    display_unit = "unit unconfirmed" if unit_label == "unknown" else unit_label
    e2.metric("Max weight", f"{max_weight:g} ({display_unit})" if max_weight is not None else "—")
    best_e1rm = summary.get("best_estimated_1rm")
    e3.metric(
        "Best estimated 1RM", f"{best_e1rm:g} ({display_unit})" if best_e1rm is not None else "—"
    )
    progress_rows = [
        {"date": session["date"], **set_row}
        for session in reversed(history.get("sessions", []))
        for set_row in session["sets"]
    ]
    progress_df = pd.DataFrame(progress_rows)
    if not progress_df.empty:
        e1rm_df = progress_df.dropna(subset=["estimated_1rm"])
        if not e1rm_df.empty:
            st.line_chart(
                e1rm_df.groupby("date", as_index=True)["estimated_1rm"].max(),
                y_label=f"estimated 1RM ({display_unit})",
            )
        st.dataframe(
            progress_df[
                ["date", "set_order", "weight", "weight_unit", "reps", "rpe", "estimated_1rm"]
            ],
            width="stretch",
            hide_index=True,
        )
    st.caption(history.get("note", queries.WORKOUT_LOG_NOTE))


def _render_health(conn: sqlite3.Connection, recovery: dict, sleep: dict, strain: dict) -> None:
    st.html(
        '<div class="os-page-title">Health context</div><div class="os-page-subtitle">Useful evidence for training decisions—kept separate from diagnostic noise and never presented as WHOOP parity.</div>'
    )
    if recovery.get("status") == "no_data":
        st.info("No recovery data is available yet.")
        return
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric(
        "Recovery proxy",
        f"{recovery['recovery_proxy']:.0f}"
        if recovery["recovery_proxy"] is not None
        else "Withheld",
    )
    r2.metric("Confidence", recovery["confidence"]["level"].title())
    r3.metric(
        "RMSSD", f"{recovery['rmssd_ms']:.1f} ms" if recovery["rmssd_ms"] is not None else "—"
    )
    r4.metric(
        "Resting HR", f"{recovery['rhr_bpm']:.0f} bpm" if recovery["rhr_bpm"] is not None else "—"
    )
    r5.metric(
        "Coverage",
        f"{recovery['coverage_pct'] * 100:.0f}%" if recovery["coverage_pct"] is not None else "—",
    )
    st.caption(recovery["note"])

    h1, h2 = st.columns(2, gap="large")
    with h1:
        st.markdown("#### Latest sleep")
        if sleep.get("status") == "no_data":
            st.info("No sleep data yet.")
        else:
            s1, s2 = st.columns(2)
            s1.metric(
                "Sleep",
                f"{sleep['sleep_minutes']} min" if sleep["sleep_minutes"] is not None else "—",
            )
            s2.metric(
                "Coverage",
                f"{sleep['coverage_pct'] * 100:.0f}%" if sleep["coverage_pct"] is not None else "—",
            )
            if sleep["segments"]:
                st.dataframe(pd.DataFrame(sleep["segments"]), width="stretch", hide_index=True)
            st.caption(sleep["note"])
    with h2:
        st.markdown("#### Latest strain")
        if strain.get("status") == "no_data":
            st.info("No strain data yet.")
        else:
            st.metric(
                "Strain proxy",
                f"{strain['strain_proxy']:.1f}"
                if strain["strain_proxy"] is not None
                else "Withheld",
            )
            zones = strain.get("time_in_zones_min") or {}
            if zones:
                st.bar_chart(
                    pd.DataFrame({"zone": list(zones), "minutes": list(zones.values())}).set_index(
                        "zone"
                    ),
                    y_label="minutes",
                )
            st.caption(strain["note"])

    now_utc = pd.Timestamp.now(tz="UTC")
    end_date = now_utc.strftime("%Y-%m-%d")
    start_date = (now_utc - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    trend1, trend2 = st.columns(2, gap="large")
    with trend1:
        st.markdown("#### HRV trend · 30 days")
        hrv = queries.get_hrv(conn, start_date, end_date)
        if hrv.get("status") == "no_data":
            st.info("No HRV trend data yet.")
        else:
            st.line_chart(pd.DataFrame(hrv["series"]).set_index("date"))
        st.caption(queries.HRV_NOTE)
    with trend2:
        st.markdown("#### Strain trend · 30 days")
        strain_df = pd.read_sql_query(
            "SELECT date, strain_proxy FROM metrics_daily "
            "WHERE date BETWEEN ? AND ? AND strain_proxy IS NOT NULL ORDER BY date",
            conn,
            params=(start_date, end_date),
        )
        if strain_df.empty:
            st.info("No strain trend data yet.")
        else:
            st.line_chart(strain_df.set_index("date"))
        st.caption(queries.STRAIN_NOTE)


def _render_system(conn: sqlite3.Connection, status: dict) -> None:
    st.html(
        '<div id="system-diagnostics" class="os-page-title">System diagnostics</div><div class="os-page-subtitle">Capture health and raw proxy detail for troubleshooting—not the daily coaching surface.</div>'
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Last reading (UTC)", status["last_reading_utc"] or "—")
    c2.metric("HR samples", status["rows"]["hr_samples"])
    c3.metric("RR samples", status["rows"]["rr_intervals"])
    c4.metric("Schema", status["schema_version"])
    c5.metric("Battery", f"{status['battery_pct']}%" if status["battery_pct"] is not None else "—")
    if status["battery_note"]:
        st.caption(status["battery_note"])

    gaps = status["coverage_gaps_24h"]
    if gaps:
        st.warning(f"{len(gaps)} connection gap(s) in the last 24 hours")
        gap_df = pd.DataFrame(gaps)
        gap_df["start_dt"] = pd.to_datetime(gap_df["start"], utc=True)
        now_utc = pd.Timestamp.now(tz="UTC")
        gap_df["end_dt"] = pd.to_datetime(gap_df["end"], utc=True).fillna(now_utc)
        gap_df["duration_min"] = (gap_df["end_dt"] - gap_df["start_dt"]).dt.total_seconds() / 60.0
        st.bar_chart(gap_df.set_index("start_dt")["duration_min"], y_label="gap duration (min)")
        st.dataframe(gap_df[["start", "end", "duration_min"]], width="stretch", hide_index=True)
    else:
        st.success("No connection gaps in the last 24 hours")

    hours = st.slider("Recent heart-rate window", min_value=1, max_value=48, value=8)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    hr_data = queries.query_readings(conn, "hr", start_ms, end_ms, limit=20_000)
    if hr_data["count"]:
        hr_df = pd.DataFrame(hr_data["samples"])
        hr_df["ts"] = pd.to_datetime(hr_df["ts"])
        st.line_chart(hr_df.set_index("ts")["value"])
        st.caption(f"{hr_data['count']} samples")
    else:
        st.info("No HR data in this window.")

    rr_data = queries.query_readings(conn, "rr", start_ms, end_ms, limit=20_000)
    st.markdown("#### RR interval distribution")
    if rr_data["count"]:
        rr_values = np.array([sample["value"] for sample in rr_data["samples"]])
        counts, bin_edges = np.histogram(rr_values, bins=30)
        st.bar_chart(
            pd.DataFrame({"rr_ms": bin_edges[:-1].round(0), "count": counts}).set_index("rr_ms")
        )
        st.caption(
            f"{rr_data['count']} raw RR samples (median {np.median(rr_values):.0f} ms)—not yet cleaned by the ectopic filter."
        )
    else:
        st.info("No RR data in this window.")

    st.markdown("#### Recent daily metrics")
    daily_df = pd.read_sql_query(
        "SELECT date, rhr_bpm, rmssd_ms, sdnn_ms, recovery_proxy, provisional, strain_proxy, "
        "sleep_minutes, coverage_pct, timezone FROM metrics_daily ORDER BY date DESC LIMIT 14",
        conn,
    )
    st.dataframe(daily_df, width="stretch", hide_index=True)


def render() -> None:
    st.set_page_config(
        page_title="Doop Training · Training OS", layout="wide", initial_sidebar_state="collapsed"
    )
    _inject_theme()
    conn = get_connection()
    try:
        today = datetime.now(local_timezone()).date()
        week_start = today - timedelta(days=today.weekday())
        guidance = queries.get_training_guidance(conn, today.isoformat())
        active_plan = queries.get_active_plan(conn)
        weekly = queries.get_weekly_training_review(conn, week_start.isoformat())
        recovery = queries.get_latest_recovery(conn)
        recovery_date = recovery.get("date") or today.isoformat()
        sleep = queries.get_sleep(conn, recovery_date)
        strain = queries.get_strain(conn, recovery_date)
        status = queries.get_status(conn, str(DEFAULT_DB_PATH))
        freshness = system_freshness(status)
        first_workout_date, last_workout_date, workout_count = conn.execute(
            "SELECT MIN(local_date), MAX(local_date), COUNT(*) FROM workout_sessions"
        ).fetchone()

        workspace = _render_header(today, freshness)
        primary = (guidance.get("planned") or [None])[0]
        today_workout = primary.get("name") if primary else None
        if workspace == "Today":
            _render_today(conn, guidance, active_plan, weekly, today, recovery, sleep)
        elif workspace == "Plan & log":
            _render_plan_and_log(conn, active_plan, weekly, today_workout)
        elif workspace == "Progress":
            _render_progress(conn, first_workout_date, last_workout_date, workout_count)
        elif workspace == "Health":
            _render_health(conn, recovery, sleep, strain)
        else:
            _render_system(conn, status)

        st.html(
            '<footer class="os-footer"><strong>Doop Training</strong><span>Local workout logbook and confidence-aware health context. Biometrics refine training; they do not replace training history.</span></footer>'
        )
    finally:
        conn.close()


if __name__ == "__main__":
    render()
