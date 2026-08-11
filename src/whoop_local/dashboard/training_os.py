"""Pure presentation helpers for the Training OS dashboard.

The dashboard may change hierarchy and tone, but it must never reinterpret
Doop's structured plan, completion, confidence, or safety state.  These helpers
turn those existing contracts into small view models that are easy to test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def _title_name(name: str) -> str:
    return name.replace("-", " ").upper()


def rep_range(exercise: dict) -> str:
    """Return an exact human-readable set/rep prescription."""
    reps = exercise.get("reps") or {}
    minimum = reps.get("min")
    maximum = reps.get("max")
    if minimum is None:
        rep_text = "—"
    elif minimum == maximum:
        rep_text = f"{minimum:g}"
    else:
        rep_text = f"{minimum:g}–{maximum:g}"
    return f"{exercise.get('sets_target', 0)} × {rep_text}"


def build_decision(guidance: dict) -> dict:
    """Build the hero copy without changing the structured recommendation."""
    planned = guidance.get("planned") or []
    primary = planned[0] if planned else None
    recommendation = guidance.get("recommendation") or {}
    recovery = guidance.get("recovery") or {}
    confidence = recovery.get("confidence") or {"level": "low", "actionable": False}
    action = recommendation.get("action")

    if primary is None:
        headline = "RECOVER TODAY. KEEP THE WEEK MOVING."
        quote = "No bonus punishment. Recover on purpose and come back ready."
        session = "Rest / easy recovery"
        status = "No session planned"
        cta = None
    elif primary.get("completed"):
        headline = f"REVIEW TODAY'S {_title_name(primary['name'])} SESSION"
        quote = "Work is logged. Take the win, then use the evidence."
        session = primary["name"]
        status = "Completed · Canonical log"
        cta = "Review today's session"
    elif action == "reduce_session":
        headline = f"RUN THE LIGHTER {_title_name(primary['name'])} SESSION"
        quote = "We keep the habit and trim the cost. Smart work still counts."
        session = primary["name"]
        status = "Planned · Not yet logged"
        cta = "Open today's session"
    elif action == "easy_or_skip":
        headline = "RECOVER TODAY. THE PLAN CAN WAIT."
        quote = "Protect the next good week. Ego does not get a vote today."
        session = primary["name"]
        status = "Planned · Adjustment advised"
        cta = "Review today's guidance"
    else:
        headline = f"DO THE PLANNED {_title_name(primary['name'])} SESSION"
        quote = "Execute the plan, leave a few in reserve, and protect the long game."
        session = primary["name"]
        status = "Planned · Not yet logged"
        cta = "Open today's session"

    recovery_value = recovery.get("value")
    recovery_label = "Withheld" if recovery_value is None else f"{recovery_value:.0f}"
    actionable_label = "Actionable" if confidence.get("actionable") else "Not actionable"
    evidence = [recommendation.get("message") or "Follow the written plan."]
    if primary and primary.get("kind") == "strength":
        evidence.extend(("Keep 2–3 reps in reserve", "Use controlled technique"))

    return {
        "headline": headline,
        "quote": quote,
        "session": session,
        "intensity": primary.get("intensity") if primary else "easy",
        "duration": primary.get("estimated_minutes") if primary else None,
        "status": status,
        "recovery": recovery_label,
        "confidence": f"{str(confidence.get('level', 'low')).title()} / {actionable_label}",
        "evidence": evidence,
        "cta": cta,
        "primary": primary,
    }


def build_week_rows(active_plan: dict, weekly: dict, today: date) -> list[dict]:
    """Merge the intended plan and current-week completion evidence."""
    week_start = today - timedelta(days=today.weekday())
    planned_by_weekday = {
        int(workout["weekday"]): workout for workout in active_plan.get("workouts", [])
    }
    evidence_by_date = {
        slot["date"]: slot for slot in (weekly.get("adherence") or {}).get("slots", [])
    }
    rows = []
    for weekday in range(7):
        current = week_start + timedelta(days=weekday)
        workout = planned_by_weekday.get(weekday)
        evidence = evidence_by_date.get(current.isoformat(), {})
        if workout is None:
            name = "Rest"
            intensity = "—"
            minutes = None
            state = "Rest"
        else:
            name = workout["name"]
            intensity = workout["intensity"]
            minutes = workout.get("estimated_minutes")
            if evidence.get("completed"):
                state = "Completed"
            elif workout.get("optional"):
                state = "Optional"
            elif current < today:
                state = "No matching log"
            elif current == today:
                state = "Today"
            else:
                state = "Upcoming"
        rows.append(
            {
                "date": current,
                "day": current.strftime("%a").upper(),
                "date_number": current.day,
                "name": name,
                "intensity": intensity,
                "minutes": minutes,
                "state": state,
                "is_today": current == today,
            }
        )
    return rows


def system_freshness(status: dict, now: datetime | None = None) -> dict:
    """Summarize freshness without claiming that every diagnostic is healthy."""
    raw = status.get("last_reading_utc")
    if not raw:
        return {"label": "No recent readings", "tone": "attention"}
    observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    age = current.astimezone(UTC) - observed.astimezone(UTC)
    if age > timedelta(minutes=10):
        return {"label": "Data sync is stale", "tone": "attention"}
    battery = status.get("battery_pct")
    if status.get("battery_low"):
        return {"label": f"Data current · Battery {battery}% low", "tone": "attention"}
    battery_text = f" · Battery {battery}%" if battery is not None else ""
    return {"label": f"Data current{battery_text}", "tone": "ok"}
