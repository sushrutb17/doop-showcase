# Evidence manifest

This manifest is the release index for the v1.0 portfolio artifact. Each
résumé claim and public headline is backed by a specific file, test, or
recorded hardware result. Hardware results are historical evidence from the
owner's device; the public repository reproduces the software invariants with
synthetic, data-free tests.

## Release identity

| Field | Value |
|---|---|
| Release | v1.0 portfolio showcase; Python package remains experimental at 0.1.0 |
| Artifact | Method-and-evidence repository, not a runnable health product |
| License | Apache-2.0 (`LICENSE`) |
| Privacy boundary | `tools/privacy_check.py`; no database, export, device identifier, credential, or personal profile |
| Verification baseline | 305 Python tests, 12 OpenClaw safety-plugin tests, and 65.61% branch coverage at the rebuilt release-candidate gate |

## Claim index

| ID | Claim | Primary evidence | Verification |
|---|---|---|---|
| `PROTOCOL-D54` | The proprietary Gen4 stream was empirically validated, including framing, HR, and four RR slots; the short-sample interpretation was corrected from retained raw packets. | `src/whoop_local/capture/proprietary_protocol.py`, `src/whoop_local/capture/backfill_rr.py`, `evidence/STATIC_TRACE.md`, `CASE_STUDY.md` | `uv run pytest -q tests/test_proprietary_protocol.py tests/test_backfill_rr.py` |
| `HISTORY-D71` | Buffered history uses RTC repair, durable commit-before-ACK handling, resumability, and typed replay deduplication. | `src/whoop_local/capture/history_sync.py`, `evidence/HARDWARE_RESULTS.md`, `CASE_STUDY.md` | `uv run pytest -q tests/test_history_sync.py` |
| `METRICS-D39` | Independent HRV, resting-HR, sleep, recovery, and strain proxies are retained for audit, while unsafe or low-quality values are withheld from coaching. | `src/whoop_local/compute/`, `src/whoop_local/mcp_server/queries.py`, `METRICS.md`, `CASE_STUDY.md` | `uv run pytest -q tests/test_metrics.py tests/test_compute_daily.py tests/test_training_insights.py` |
| `AGENT-ACCEPTANCE` | BROC receives minimum-disclosure training relays, while pain, illness, and unavailable-data cases fail closed before model inference; both promotion revocations are published. | `integrations/openclaw/ACCEPTANCE.md`, `src/whoop_local/integrations/openclaw_acceptance.py`, `integrations/openclaw/plugins/doop-safety-gate/` | `uv run pytest -q tests/test_openclaw_acceptance.py && node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js` |
| `WRITE-D47` | A workout draft remains separate from a canonical completion until an explicit local confirmation occurs. | `evidence/D47_CONFIRMATION.md`, `src/whoop_local/manual_logging/`, `tests/test_manual_logging.py` | `uv run pytest -q tests/test_manual_logging.py` |
| `TRACE-V1` | A synthetic CRC-valid packet can be followed through parsing, real daily computation, bounded MCP output, and the visible BROC answer. | `evidence/STATIC_TRACE.md`, `tools/create_film_seed.py`, `tests/test_film_seed.py` | `uv run pytest -q tests/test_film_seed.py` |
| `PRIVACY-V1` | The published tree is safe to inspect without exposing private health or runtime data. | `tools/privacy_check.py`, `README.md`, `THIRD_PARTY_NOTICES.md` | `python3 tools/privacy_check.py` |
| `VIDEO-V1` | The demonstration presents the accepted agent path using only synthetic seed data and privacy-reviewed screen footage. | `VIDEO_STORYBOARD.md` plus the release video asset or linked release attachment | Manual frame-by-frame review against the published recording contract |

## Final acceptance command

Run from a fresh clone with no database, band, credentials, or private runtime:

```sh
uv sync --all-extras --locked
python3 tools/privacy_check.py
uv run ruff check src tests tools
uv run ruff format --check src tests tools
uv run mypy src/whoop_local
uv run pytest --cov=whoop_local --cov-report=term-missing -q
uv run pip-audit --local --progress-spinner=off
node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js
```

The exact final Python and plugin test counts must be recorded in the release
notes after this command completes. The manifest intentionally separates
reproducible commands from private-hardware results that cannot be replayed by
a reviewer without the original device.

## Release artifacts

- `site/` and [doop-showcase.vercel.app](https://doop-showcase.vercel.app) — responsive public project overview.
- `VIDEO_STORYBOARD.md` — exact four-beat recording, narration, and privacy-review contract.
- `RELEASE_NOTES.md` — exact clean-clone results and final tag checklist.
- `README.md` — reviewer entry point and architecture visual.
- `REVIEWER_CHECKLIST.md` — ten-minute claim → evidence → command path.
- `CASE_STUDY.md` — investigation narrative, limitations, and provenance.
- `evidence/STATIC_TRACE.md` — one-page raw → coaching trace.
- `integrations/openclaw/ACCEPTANCE.md` — sanitized agent acceptance record and revocations.
- `RESUME_BULLETS.md` — résumé language tied to this manifest.
- `LICENSE` and `THIRD_PARTY_NOTICES.md` — publication terms and attribution.
- `VIDEO-V1` — privacy-reviewed BROC demonstration, released as a repository asset or release attachment.
