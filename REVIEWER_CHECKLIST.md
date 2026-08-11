# Ten-minute reviewer checklist

This repository makes two headline claims: it independently validated and
extended a reverse-engineered WHOOP 4.0 BLE pipeline, and it built a bounded
training-domain agent around that substrate. Use this checklist to inspect each
claim without a device, database, credential, or model runtime.

For the release-level index tying claims to résumé language and acceptance
artifacts, see [EVIDENCE_MANIFEST.md](EVIDENCE_MANIFEST.md).

| Claim | Evidence | Independent check |
|---|---|---|
| The public tree is privacy-safe and data-free | `tools/privacy_check.py`; README privacy contract | `python3 tools/privacy_check.py` |
| Gen4 frames, checksums, HR, and four RR slots are implemented from explicit wire facts | `src/whoop_local/capture/proprietary_protocol.py`; `evidence/STATIC_TRACE.md` | `uv run pytest -q tests/test_proprietary_protocol.py` |
| Raw-first storage makes later decoder corrections recoverable | `src/whoop_local/capture/backfill_rr.py`; D54 narrative in `CASE_STUDY.md` | `uv run pytest -q tests/test_backfill_rr.py` |
| Buffered history is RTC-first, commit-before-ACK, resumable, and replay-safe | `src/whoop_local/capture/history_sync.py`; `evidence/HARDWARE_RESULTS.md` | `uv run pytest -q tests/test_history_sync.py` |
| Recovery, strain, HRV, and sleep are independent computations—not WHOOP parity | `METRICS.md`; `src/whoop_local/compute/` | `uv run pytest -q tests/test_metrics.py tests/test_compute_daily.py` |
| Plausible computed values cannot influence training without sufficient evidence | D39 section in `CASE_STUDY.md`; `src/whoop_local/mcp_server/queries.py` | `uv run pytest -q tests/test_training_insights.py` |
| BROC receives minimized today/week relays rather than broad health diagnostics | `integrations/openclaw/ACCEPTANCE.md`; evaluator source | `uv run pytest -q tests/test_openclaw_acceptance.py` |
| Pain and unavailable-data turns fail closed before inference | Safety revocation history and plugin source | `node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js` |
| A workout draft cannot silently become a canonical completion | `evidence/D47_CONFIRMATION.md`; manual logging service | `uv run pytest -q tests/test_manual_logging.py` |
| The static raw → coaching story uses real shipped transformations on synthetic inputs | `evidence/STATIC_TRACE.md`; film-seed source | `uv run pytest -q tests/test_film_seed.py` |

For recorded physical results that cannot be republished without exposing
personal health data, read `evidence/HARDWARE_RESULTS.md`. It explicitly labels
the boundary between a recorded owned-device acceptance result and a software
invariant reproducible in this repository.

To run the complete acceptance surface:

```sh
uv sync --all-extras --locked
python3 tools/privacy_check.py
uv run pytest -q
node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js
```
