# Reverse-engineering a WHOOP 4.0: method, evidence, and what it taught me

## The question

Could a personally owned WHOOP 4.0 still be useful without a subscription if
its locally available BLE data were captured, decoded, and processed on the
owner's hardware?

Doop answers a bounded version of that question. It reads the band's local BLE
stream, preserves raw packets, derives independent HRV, resting-HR, recovery,
strain, and sleep estimates, and exposes confidence-gated summaries to BROC, a
training-domain agent. It does not use WHOOP's cloud, claim score parity, or
turn a consumer wearable into a medical device.

The contribution is not “I invented this protocol from nothing.” Community
research suggested some wire facts. The engineering work was deciding what to
trust, testing each fact on owned hardware, rejecting contradictions, retaining
raw evidence, and building failure-safe storage, compute, and agent boundaries
around what survived.

## Boundary before bytes

The work stayed inside an owner-controlled device and local computer:

- no WHOOP account, cloud API, authentication bypass, or vendor server;
- passive GATT reconnaissance before command writes;
- one low-risk reversible stream toggle before broader protocol work;
- raw health data, exports, identifiers, and operational logs kept private;
- only sanitized source, synthetic packets, tests, and qualitative field
  outcomes published here.

The public repository is therefore a method-and-evidence artifact, not a dump
of someone’s biometric history. The privacy checker enforces the boundary, and
the [hardware evidence ledger](evidence/HARDWARE_RESULTS.md) distinguishes
recorded private-hardware results from claims a stranger can reproduce using
the data-free suite.

Numerical validation from the owner's devices remains private. Public
reproducibility is deliberately limited to software invariants exercised by
synthetic fixtures; none of the field observations below is a vendor-parity or
clinical-validation claim.

## 1. Start with the easy path—and let it fail

The band advertised the standard BLE Heart Rate service and its Heart Rate
Measurement characteristic had a notification descriptor. The first plan was
deliberately boring: subscribe to `0x2A37` and avoid proprietary decoding.

It produced no measurements. The result was reproduced with both the initial
collector and nRF Connect while the band was worn and notifications were
enabled. A visible standard service was not a working data path on this
firmware. That negative result changed the project from an integration task to
a protocol investigation.

The own-device GATT scan showed a proprietary service at `61080001-…` with
characteristics `61080002` through `61080005` and `61080007`. One community
source described a different service base and a 96-byte packet with scaled
heart rate. It was rejected: both claims contradicted the scan and, later, the
actual 20-byte stream. Another trail—`bWanShiTong/openwhoop`, encountered
through `b-nnett/goose`—matched the scan exactly and suggested the Gen4 frame
shape. That made it a hypothesis worth testing, not ground truth.

## 2. Prove framing before interpreting payloads

The candidate Gen4 frame was:

```text
[0xAA] [declared length: u16 little-endian] [CRC-8 of length]
[payload]
[CRC-32 of payload: u32 little-endian]
```

Passive proprietary events validated both checksums before any command was
sent. Then command `3` was written to `61080002`; a CRC-valid response arrived
on `61080003`, followed by a regular stream on `61080005`.

In the first short capture, payload byte 8 moved smoothly with plausible heart
rate while the other changing bytes behaved like counters. It decoded directly
as an unsigned BPM value—no scaling. A longer capture revealed that byte 9 was
an RR count and bytes 10–13 held up to two little-endian RR intervals in
milliseconds. The observed non-zero intervals passed the project's generic
physiological cleaning bounds; their values and counts remain private.

The important practice was preserving every notification before decoding it.
`raw_packets` was append-first evidence; `hr_samples`, `rr_intervals`, and
`metrics_daily` were regenerable interpretations.

## 3. A short sample lied by omission

Bytes 14–17 stayed zero in the early captures and were tentatively treated as
SpO₂ or temperature candidates. A later full re-scan of the stored realtime
packets showed the error. Some packets carried three or four intervals. Bytes
14–15 and 16–17 were RR slots three and four, and the observed values passed
the same generic bounds as slots one and two.

Because the raw packets still existed, the parser could be corrected and the
previously missed RR values re-derived. A full metric recompute did not
materially alter the established qualitative interpretation. The record-level
totals and before/after physiological measurements remain in the private
engineering log; the public idempotence claim is covered by
`tests/test_backfill_rr.py` using generated packets.

This was also a clean negative result: every byte in the 20-byte realtime
payload was now accounted for, so SpO₂ and skin temperature were not hiding in
it. Finding those signals would require a different stream or command. That
research was intentionally stopped rather than manufacturing an answer.

## 4. Real nights corrected plausible algorithms

The metric layer computes RMSSD/SDNN from cleaned RR intervals, resting heart
rate from low-HR windows, coarse sleep from sustained low/stable HR, and
independent recovery and strain proxies. It initially looked correct under
synthetic tests. Overnight validation then showed that requiring one perfectly
unbroken low/stable block could miss an otherwise credible sleep period:
ordinary arousals and movement fragmented the qualifying signal.

The fix joins qualifying blocks across a configurable brief-interruption
window before applying the minimum duration. Re-running the preserved night
produced a plausible but conservative estimate. The numerical comparison stays
private; generated timelines in `tests/test_metrics.py` verify both bridging
and refusal to bridge longer gaps.

Daily attribution also moved from UTC-midnight rows to local noon-to-noon
windows, crediting overnight sleep to the wake-up date. This avoided splitting
one human night across two calendar answers.

## 5. History download failed for the right reasons

Live BLE range is not enough for everyday use, so buffered-history recovery was
the hardest protocol step.

Commands `34` (data range) and `22` (send historical data) returned CRC-valid
responses from the band, but an empty history request produced console logs and
no biometric records. A decoded range response exposed a page counter and a
steadily advancing device clock—but on an old epoch. A proposed
`[start_page, end_page)` request was tested and disproved: it returned the same
diagnostic dump.

The logs contained the decisive clue: the RTC was invalid and the firmware was
not saving data to flash. Comparing “newest record” to the device clock showed a
substantial write gap. This was not evidence of a particular buffer capacity;
it was evidence that retention capacity cannot recover records never written.

The working flow, corroborated by MIT-licensed OpenStrap research and then
tested on the owner's band, became:

1. set and read back the RTC;
2. send the exact padded five-step Gen4 initialization;
3. reassemble START/END/COMPLETE metadata and typed history records;
4. commit raw and decoded data before acknowledging the exact cursor;
5. resume without aborting while a long transfer continues to advance;
6. deduplicate typed replay while retaining raw transport evidence.

After RTC repair, the drain reached `HISTORY_COMPLETE` and recovered typed HR
and RR history. Recomputing produced recovered daily metrics, and replay
remained idempotent. Exact record totals stay private. The earlier
diagnostic-only region remained visible as failure evidence rather than being
reinterpreted as health data. The public state-machine and deduplication
invariants are exercised with synthetic history frames in
`tests/test_history_sync.py`.

## 6. The operating system became part of the experiment

A collector that parses perfectly can still lose data. macOS CoreBluetooth
left a pending connection unresolved indefinitely: no exception, no disconnect
callback, and therefore no recovery by the streaming watchdog. Setup and
disconnect were put under bounded deadlines, and startup now reconciles
orphaned capture-gap rows. A separate data-freshness watchdog remains
responsible only after streaming starts.

This distinction matters because data quality is part of the product contract,
not just operations. An orphaned open gap once corrupted later coverage
accounting; a silent connection hang produced a plausible recovery number from
grossly incomplete input.

## 7. Validate the substrate with a second observer

Apple Watch exports were imported beside Doop data, never blended into it. In
the private overlapping-wear review, the two sources were directionally
consistent during well-covered periods. The largest discrepancies occurred
when the band contributed sparse observations—a coverage problem, not evidence
for silently calibrating one device to the other. Exact overlap duration,
paired deltas, sample counts, and physiological values remain private.

The Watch was an observer, not truth. Its sleep segments and workouts remain
source-labeled context; they do not overwrite WHOOP-derived metrics. This
limited single-owner check does not establish accuracy or parity with either
vendor.

## 8. Computed is not actionable

The most dangerous output was not a parser error. It was a convincing recovery
score computed from grossly incomplete coverage and no detected sleep. A prose
caveat was not enough because the number still dominated the interface.

Doop therefore stores computed values for audit but withholds them from
decision surfaces unless they pass explicit, configurable gates for coverage,
sleep, input completeness, and baseline maturity. MCP returns structured
reasons when a scalar is withheld. Synthetic tests in `tests/test_mcp_queries.py`
verify those software invariants without publishing the triggering day.

BROC sees even less: two deterministic, minimum-disclosure training relays
rather than raw readings or diagnostic status. The published
[agent acceptance record](integrations/openclaw/ACCEPTANCE.md) shows why—the
surface was revoked once for returning `NO_REPLY` to a sharp-pain report and again for
analyzing capture-gap timestamps after the correct relay.

The [static end-to-end trace](evidence/STATIC_TRACE.md) follows a generated
CRC-valid packet through the shipped parser, daily compute path, MCP JSON, and
the verbatim relay answer without exposing a real biometric packet.

## Provenance: suggestion is not confirmation

None of the four early community protocol-research repositories granted a
license; three were no longer reachable at the publication audit. No file,
capture, fixture, or source code from them is shipped. The project retains only
protocol facts independently checked on owned hardware. Full license status is
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

| Borrowed or suggested fact | Source that suggested it | Independent project evidence |
|---|---|---|
| Proprietary GATT UUID family | `bWanShiTong/openwhoop`, via `b-nnett/goose` | Own nRF Connect scan matched service `61080001`, chars `61080002`–`61080005`, and `61080007` exactly |
| Gen4 `0xAA` framing with CRC-8 length and CRC-32 payload | Same `openwhoop_reference.rs` trail | Hand-checked offsets, then validated both CRCs on passive owned-band events and every accepted stream/history frame |
| Approximate characteristic roles | `jogolden/whoomp`; `bWanShiTong/reverse-engineering-whoop` | Command write produced an echoed ACK on `61080003`; events arrived on `61080004`; realtime data arrived on `61080005` |
| Realtime toggle command `3` | OpenWhoop-derived reference material | Exact command/sequence returned a CRC-valid ACK, then a sustained stream on the owned Gen4 band |
| History commands `34`, `22`, and cursor ACK `23` | Community history notes; later OpenStrap | Commands 34/22 returned CRC-valid echoed responses; full RTC-first flow advanced cursors and reached `HISTORY_COMPLETE` |
| Padded Gen4 history initialization and metadata markers | OpenStrap `protocol`/`edge` (MIT) | Exact sequence recovered typed history and replay remained idempotent; private totals are omitted |
| Individual semantic names for wire values 35/76/67 | Borrowed reference names | Semantics were not proven, so publication retains the accepted values only as neutral ordered initialization steps |
| 96-byte realtime layout and scaled HR | `christianmeurer/whoop-reader` candidate | Rejected: own scan and actual CRC-valid stream showed a different service mapping and a 20-byte payload with uint8 BPM |

The HR byte, four RR slots, RTC failure diagnosis, and negative SpO₂/temperature
result were derived from this project's own captures rather than accepted from
the table above.

## Why stop here

The original practical goal was daily training utility without a subscription.
That utility is now available from supported commercial devices with less
maintenance. Continuing into motion decoding, new proprietary streams,
always-on hardware, or broader chatbot actions would enlarge the system without
materially strengthening the demonstrated method.

So v1.0 closes on evidence: protocol investigation, raw-first correction,
history durability, independent validation, confidence gating, and a bounded
agent. A Garmin comparison was scoped as a possible later observer, not a
condition of completion. It was not performed for v1.0.

## Limitations

- Recovery, strain, and sleep are independent proxies, not WHOOP parity,
  Garmin parity, medical advice, or ground truth.
- The strain proxy is v1 and uncalibrated. It sums across the whole day, so a
  rest day can still produce a non-trivial value; its scale is not trustworthy
  enough for strain-driven coaching. “Implemented” does not mean calibrated.
- Personal z-score baseline normalization is implemented and synthetically
  tested but remains physiologically unvalidated at scale.
- Sleep detection is HR-only. Motion/accelerometer decoding was not attempted,
  and the output provides no clinical sleep staging.
- Sparse BLE coverage can bias comparisons and daily metrics; the confidence gate withholds
  unsafe decisions but cannot recreate data that a dead battery or invalid RTC
  never recorded.
- The Apple Watch comparison is a private, single-owner directional check, not
  population evidence, clinical validation, or a vendor-parity claim.
- The optional Garmin comparison was scoped and not performed.
- Published field outcomes are qualitative. Real packet captures and numerical
  validation remain private, so external reviewers can reproduce the software
  invariants and synthetic trace but not independently replay the owner's band.

That final limitation is deliberate. A privacy-safe case study should show
enough to evaluate the method without making personal health data the price of
credibility.
