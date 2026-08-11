# Static raw → coaching trace

This trace was regenerated from a newly created isolated film seed using the
shipped production functions. Every date, timestamp,
physiological value, and training value below is a deliberately synthetic
fixture. No captured health data is present.

The seed generated eight days directly at the fixed fictional 2037-07-06
epoch, retained 392 CRC-valid synthetic packets, parsed them through
`proprietary_protocol.py`, and called `compute_for_date` for each day. The
values below were copied from that run; they were not shifted from an older
trace or hand-calculated.

## 1. Raw notification retained first

The selected record is the first synthetic notification generated for
2037-07-06. The full framed bytes were stored verbatim before typed rows were
derived:

```text
characteristic: 61080005-8d6d-82b8-614a-1c8cb0f8dcc6
timestamp_ms:   2130454800000
frame_hex:      aa1800ff2802bc020000000035042b0335032b03350301016979cf26
```

Byte-level split:

```text
aa | 1800 | ff | 2802bc020000000035042b0335032b0335030101 | 6979cf26
^    ^      ^    ^                                          ^
SOF  length CRC8 payload                                    CRC32
```

## 2. Production parser output

`parse_frame`, `parse_realtime_hr`, and `parse_realtime_rr` returned:

```json
{
  "packet_type": 40,
  "header_crc_valid": true,
  "payload_crc_valid": true,
  "payload_hex": "2802bc020000000035042b0335032b0335030101",
  "hr_bpm": 53,
  "rr_ms": [811.0, 821.0, 811.0, 821.0]
}
```

The relevant synthetic payload fields are byte 8 = `0x35` = 53 BPM, byte 9
= `0x04` RR values, and four little-endian RR slots at bytes 10–17.

## 3. Daily computation

This packet was one of 49 synthetic five-minute samples for the selected day.
The day was computed with the production aggregation path after seven earlier
synthetic days established the baseline:

```json
{
  "date": "2037-07-06",
  "rmssd_ms": 14.087637788555556,
  "sdnn_ms": 11.20710322706184,
  "rhr_bpm": 53.0,
  "sleep_minutes": 180,
  "recovery_proxy": 50.0,
  "strain_proxy": 9.605245379107913,
  "coverage_pct": 1.0,
  "provisional": false
}
```

Recovery 50 means “at this synthetic baseline,” not 50% recovered and not a
WHOOP score. The synthetic strain comes from the fixture's designed elevated
heart-rate tail and its explicit film-only heart-rate context; it is not an
individual calibration or a claim about a real person.

## 4. MCP audit JSON and decision gate

The local `get_latest_recovery` query read the computed row and applied D39:

```json
{
  "date": "2037-07-06",
  "recovery_proxy": 50.0,
  "rmssd_ms": 14.087637788555556,
  "hrv_baseline_ms": 14.087637788555556,
  "rhr_bpm": 53.0,
  "sleep_minutes": 180,
  "provisional": false,
  "coverage_pct": 1.0,
  "confidence": {
    "level": "high",
    "actionable": true,
    "reasons": []
  },
  "note": "Independent proxy; not a WHOOP recovery score."
}
```

The owner-facing agent does not receive that broad audit object. Its accepted
tool is the narrower `get_telegram_training_guidance` relay. The actual MCP
result from the same fresh seed was:

```json
{
  "date": "2037-07-06",
  "relay_contract": "Return text verbatim as the visible answer. Do not call another tool or add health facts.",
  "text": "Doop training guidance:\nPlanned; not yet logged as completed.\nFoundation A — moderate — about 55 minutes.\nPlan instructions: Fictional template with two full-body sessions, one aerobic-base session, and optional mobility. Keep 2–3 reps in reserve and stop well before failure. This is an intended prescription, not a completed workout log.\nGoblet Squat (Dumbbell): 3 × 8–10 @ 16 kg; tempo 3010; rest 90s.\nPush Up: 3 × 8–12; tempo 2010; rest 75s.\nSeated Row (Cable): 3 × 8–12 @ 25 kg; tempo 2011; rest 75s.\nRomanian Deadlift (Dumbbell): 2 × 8–10 @ 14 kg; tempo 3010; rest 90s. The listed load is per dumbbell.\nRecovery confidence: high (actionable). Recovery is adequate. Perform the session as written and keep 2–3 reps in reserve.\nPain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides this guidance."
}
```

## 5. BROC's visible answer

The accepted agent contract returns the relay text verbatim:

```text
Doop training guidance:
Planned; not yet logged as completed.
Foundation A — moderate — about 55 minutes.
Plan instructions: Fictional template with two full-body sessions, one aerobic-base session, and optional mobility. Keep 2–3 reps in reserve and stop well before failure. This is an intended prescription, not a completed workout log.
Goblet Squat (Dumbbell): 3 × 8–10 @ 16 kg; tempo 3010; rest 90s.
Push Up: 3 × 8–12; tempo 2010; rest 75s.
Seated Row (Cable): 3 × 8–12 @ 25 kg; tempo 2011; rest 75s.
Romanian Deadlift (Dumbbell): 2 × 8–10 @ 14 kg; tempo 3010; rest 90s. The listed load is per dumbbell.
Recovery confidence: high (actionable). Recovery is adequate. Perform the session as written and keep 2–3 reps in reserve.
Pain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides this guidance.
```

The trace demonstrates transformations, not causation from one packet. Daily
metrics aggregate a day of samples, and rolling baselines aggregate prior days.
The fictional plan contributes the exercise prescription. The synthetic
biometric path contributes only a confidence-gated adjustment, and no read path
logs completion or changes the plan.
