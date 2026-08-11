# Recorded hardware and field evidence

This ledger freezes the qualitative evidence used by the case study. Results
came from a personally owned WHOOP 4.0 and an independently exported Apple
Watch dataset. Raw captures, device identifiers, dates tied to the owner,
database files, and operational logs are private and are not included.

“Recorded” means the result was accepted in the private engineering log before
the showcase tree was assembled. “Reproducible here” means a reviewer can run
the linked data-free test or synthetic path. The distinction is intentional.

Numerical physiological validation, paired-observer deltas, operational
durations, and private record totals are intentionally omitted. The field
observations establish which engineering paths were worth keeping; they do not
claim clinical validity or parity with WHOOP, Apple, or any other vendor.

| Result | Recorded evidence | Public verification boundary |
|---|---|---|
| Standard Heart Rate characteristic did not emit measurements | Reproduced with the original collector and nRF Connect while worn with notifications enabled | Collector/parser code is published; physical negative result is recorded only |
| Gen4 framing accepted | Passive events and active command/data frames passed CRC-8 header and CRC-32 payload checks | `tests/test_proprietary_protocol.py` tests framing with generated frames |
| Realtime HR decode | The stream command was acknowledged and the candidate payload byte tracked plausible heart-rate changes without scaling | Parser implementation and synthetic tests are published |
| RR slots 1–2 | Non-zero intervals appeared in the first two slots and passed the configured generic cleaning bounds | Four-slot parser tests are published |
| RR slots 3–4 and negative SpO₂/temp result | A full stored-packet scan showed that the remaining candidate bytes were additional RR slots, accounting for the realtime payload | Backfill/parser code and generated count-3/count-4 frames are published |
| Raw-first correction | Previously missed RR intervals were re-derived from retained packets; the physiological before/after review remains private | `tests/test_backfill_rr.py` verifies idempotent re-derivation |
| First sleep correction | A naive unbroken-block detector missed an observed sleep period; configurable interruption bridging produced a conservative estimate | Sleep algorithm and synthetic unit tests are published |
| RTC/history failure | Console logs stated that an invalid RTC prevented flash writes; decoded range fields corroborated a substantial write gap | History state-machine tests cover failure handling; private logs remain private |
| History acceptance | The transfer reached `HISTORY_COMPLETE`, recovered typed HR/RR history, and remained idempotent on replay | `tests/test_history_sync.py` verifies commit-before-ACK, markers, and replay dedup |
| CoreBluetooth hang | A pending connection remained unresolved until setup received a bounded deadline | `tests/test_collector.py` covers timeout and gap handling |
| Second observer | The sources were directionally consistent in well-covered periods; sparse band coverage explained the largest discrepancies | Apple importer/tests are public; the private export, overlap duration, paired deltas, and sample values are not |
| Confidence gate caught unsafe presentation | A plausible recovery score had been computed from grossly incomplete coverage with no detected sleep | MCP/training tests verify scalar withholding and structured reasons |

The static public trace is synthetic by design and lives in
`evidence/STATIC_TRACE.md`. The full data-free suite is the reproducibility
claim; this table is the privacy-safe, qualitative field record. Reviewers can
reproduce the software behavior, not the owner's measurements or a vendor
accuracy comparison.
