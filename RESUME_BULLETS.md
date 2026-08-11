# Traceable résumé bullets

Each bullet is intentionally scoped to evidence in `EVIDENCE_MANIFEST.md`.

- Reverse-engineered and hardware-validated a local WHOOP 4.0 BLE pipeline,
  correcting a short-sample RR hypothesis from retained raw evidence,
  backfilling previously missed intervals, and implementing replay-safe
  buffered-history recovery through an RTC-first commit-before-ACK state
  machine. *(Manifest:
  `PROTOCOL-D54`, `HISTORY-D71`.)*

- Built an independent biometric compute and MCP access layer with explicit
  coverage, sleep, and baseline gates that withhold plausible-but-unsafe
  recovery values from training decisions while retaining them for audit and
  recomputation. *(Manifest: `METRICS-D39`.)*

- Engineered BROC's minimum-disclosure agent boundary with two deterministic
  training relays, pre-model pain/offline interception, semantic trajectory
  evaluation, and explicit local confirmation for writes; published both
  promotion revocations rather than smoothing over the failures. *(Manifest:
  `AGENT-ACCEPTANCE`, `WRITE-D47`, `VIDEO-V1`.)*
