# D47 explicit workout confirmation

The recorded confirmation flow is rendered here with the fixed fictional date
2037-07-06 against a newly created isolated film-seed database. Every value is
synthetic. The private database, services, device, and agent runtime were not
opened. Draft and row identifiers below are normalized so the evidence does
not pretend incidental IDs are part of the contract.

The payload is published as `evidence/d47_workout.example.json`. Three separate
CLI invocations exercised the existing service boundary:

```sh
uv run python -m whoop_local.manual_logging.cli --db "$ISOLATED_DB" \
  prepare --json-file evidence/d47_workout.example.json
```

Result:

```json
{
  "confirmation_required": true,
  "draft_id": "<draft-id>",
  "status": "pending",
  "workout": {
    "local_date": "2037-07-06",
    "name": "Foundation A (synthetic confirmation)",
    "exercises": [{
      "name": "Goblet Squat (Dumbbell)",
      "sets": [
        {"weight": 16.0, "weight_unit": "kg", "reps": 9.0, "rir": 3.0},
        {"weight": 16.0, "weight_unit": "kg", "reps": 9.0, "rir": 3.0},
        {"weight": 16.0, "weight_unit": "kg", "reps": 9.0, "rir": 3.0}
      ]
    }]
  }
}
```

At this point the isolated database still contained only the one previously
confirmed synthetic seed session. Preparing a draft did not create a canonical
workout.

```sh
uv run python -m whoop_local.manual_logging.cli --db "$ISOLATED_DB" \
  show '<draft-id>'
```

Result: `status` remained `pending`, `confirmation_required` remained `true`,
and `workout_session_id` remained `null`. The reviewed payload matched the
prepared payload.

Only then was confirmation invoked as a separate command:

```sh
uv run python -m whoop_local.manual_logging.cli --db "$ISOLATED_DB" \
  confirm '<draft-id>'
```

Result:

```json
{
  "draft_id": "<draft-id>",
  "status": "confirmed",
  "workout_session_id": "<session-id>"
}
```

Postcondition query: two confirmed synthetic manual sessions existed (the seed
session plus this case), the draft status was `confirmed`, and exactly three set
rows belonged to the new session. The service test suite also verifies that a
second confirmation is rejected and cannot create another workout.

This is intentionally outside chat. BROC has no general write tool; a pending
draft is not a workout, and only the separately invoked local confirmation
materializes canonical rows atomically.
