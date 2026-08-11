# Third-party notices

## Project artwork

`site/assets/hero-biometric-workbench.jpg` is a synthetic illustration created
specifically for this showcase. It is not a photograph of the owner, a
wearable, or an owner environment; it contains no personal data and was not
sourced from any project listed below. Its reviewed bytes are pinned by the
repository privacy check. The illustration is distributed with this repository
under Apache-2.0.

> Audited before publication for the Phase 10 showcase license gate (D86/D87/D88).
> Every external input to this project is listed below with what it
> contributed, whether that contribution was **code/data** or **findings**, and
> its license status at audit time.

## Summary

| Source | Contributed | Kind | License at audit |
|---|---|---|---|
| OpenStrap `protocol` + `edge` | WHOOP 4 history protocol implementation and test vectors | code/findings | **MIT** |
| `hasaneyldrm/exercises-dataset` | 1,324 prospective exercise entries (names, categories, body parts, equipment, targets, muscles, instruction text) | **data** | **MIT** (non-media only; media excluded — see below) |
| `bWanShiTong/openwhoop` (read via `b-nnett/goose`) | GATT UUID mapping, Gen4 frame structure | findings | **none; repository not reachable at audit** |
| `b-nnett/goose` | `openwhoop_reference.rs`; a captured frame used to hand-verify framing | findings | **no license file** |
| `jogolden/whoomp` | characteristic attribution | findings | **none; repository not reachable at audit** |
| `bWanShiTong/reverse-engineering-whoop` | command/response codes, capture method | findings | **none; repository not reachable at audit** |
| `christianmeurer/whoop-reader` | **nothing** — evaluated and rejected (D15) | — | n/a |
| `madhursatija/whoof` | **nothing** — bibliography only (see below) | — | not reachable at audit |
| `noop-app/noop` | **nothing** — bibliography only (see below) | — | MIT (unused) |

Nothing in the four protocol-research rows grants a license. Reliance on them is
limited to **unprotectable facts about a proprietary wire protocol** — UUID
values, frame layout, packet-type and command integers — each independently
confirmed against this project's own hardware. No third-party source file,
capture, fixture, or test vector from those repositories is vendored into this
tree. See "Protocol research sources" below for the verification trail.

---

## OpenStrap protocol and edge

The WHOOP 4 history protocol implementation and test vectors were informed by
the OpenStrap `protocol` and `edge` projects.

Copyright (c) 2026 OpenStrap

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Exercise dataset — `hasaneyldrm/exercises-dataset`

<https://github.com/hasaneyldrm/exercises-dataset>

Source of the 1,324 metadata-only prospective exercise entries in
`external_exercises` (D60/D61). This repository splits its terms, and the split
matters:

**Non-media data — used here, MIT.** Exercise names, categories, body parts,
equipment, targets, muscle groups, and instruction text/translations are
released under the MIT License:

Copyright (c) 2026 Hasan Emir Yıldırım

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation and data files (the "Software"),
to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

**Media — NOT used here, and deliberately so.** The exercise images and
animation GIFs in that repository are **not** MIT. They are the property of
**Gym visual** (<https://gymvisual.com/>), redistributed there under separate
written permission with resolution and attribution conditions, and that
repository states explicitly that cloning it grants no rights to the media.

This project's importer **excludes all media by design** (D60/D61): no image,
video, or thumbnail from that dataset is fetched, stored, or redistributed, and
`external_exercises` holds metadata only. No Gym visual asset exists anywhere in
this tree, so no Gym visual licence is required or claimed.

---

## Protocol research sources — unlicensed, findings only

Four community reverse-engineering sources informed the WHOOP 4.0 (Gen4)
protocol work. **None grants a license, and three were unreachable at their
cited paths at the pre-publication audit.** Reliance on them is therefore
deliberately limited to facts, and every borrowed fact carries independent
confirmation from this project's own hardware.

| Source | Status at pre-publication audit | Contributed |
|---|---|---|
| `bWanShiTong/openwhoop`, read via `b-nnett/goose`'s `openwhoop_reference.rs` at a pinned commit (D15) | not reachable | GATT UUID mapping; Gen4 frame structure (`0xAA` start, CRC-8 length check, CRC-32 payload check) |
| `b-nnett/goose` | reachable; **no license file** | the `openwhoop_reference.rs` citation, and one captured frame with sidecar provenance used to hand-verify byte offsets |
| `jogolden/whoomp` | not reachable | rough characteristic attribution, later checked against owned hardware |
| `bWanShiTong/reverse-engineering-whoop` | not reachable | command/response codes and capture method, later checked against owned hardware |

**What was and was not taken.** No source file, capture, fixture, or test vector
from any of these repositories is vendored into this tree — `tests/` contains
only `.py` files, and `src/whoop_local/capture/proprietary_protocol.py` consists
of protocol constants plus CRC handling via the Python standard library's
`zlib`. What was taken is factual: which UUID, which byte offset, which command
integer.

**Independent verification trail.** Each borrowed fact was confirmed against
this project's own WHOOP 4.0 before being relied upon:

- the GATT UUID mapping matched this project's own Phase 0 scan **exactly**
  (service `61080001`, characteristics `61080002`–`61080005`, memfault
  `61080007`) — the mapping was checked, not assumed (D14/D15);
- frame structure was verified by hand against a real captured frame, byte
  offsets confirmed (D15);
- `COMMAND_GET_DATA_RANGE` (34) and `COMMAND_SEND_HISTORICAL_DATA` (22) both
  returned valid CRC-correct responses echoing this project's exact
  command/sequence numbers on the owner's band (D16/D27);
- the complete padded history handshake, RTC format, metadata markers, and ACK
  cursor semantics were independently corroborated by OpenStrap's MIT-licensed
  Gen4 implementation and then accepted against real hardware (D69/D71);
- where a source's implicit understanding was **wrong**, this project corrected
  it from its own evidence: D54 proved bytes 14–17 are RR slots 3–4 across the
  retained validation corpus, and proved SpO₂/skin temperature are absent from
  the realtime payload entirely.

A candidate source, `christianmeurer/whoop-reader`, was evaluated and
**rejected** as low-confidence in D15: its claimed UUID mapping did not match
this project's own scan and its claimed 96-byte packet layout did not match the
real 20-byte realtime payload. Nothing from it was used.

**Speculative names removed; verified wire values retained (D90).** The D88
audit incorrectly described three constants as unused. Values 35, 76, and 67
are steps 1, 2, and 4 of the exact five-command history initialization sequence
that reached `HISTORY_COMPLETE` on the owner's band and is protected by exact
frame-vector tests. What was not independently established was the borrowed
semantic naming (`GET_HELLO_HARVARD`, `GET_ADVERTISING_NAME_HARVARD`, and
`GET_ALARM_TIME`). Stage 2 therefore renamed them to neutral ordered
history-initialization steps. The published tree retains only the hardware-
verified wire facts and makes no unsupported claim about those commands'
individual meanings.

**Bibliography-only entries — listed, never used.** Two additional repositories
were considered during background reading: `madhursatija/whoof`
(HRV/recovery/strain math) and `noop-app/noop` (SQLite schema and history
imports). Neither contributed anything and neither is referenced by the
published implementation or metric/schema contracts. This project's metric math is its own
(D9/D17/D32/D35) and its schema is its own. `madhursatija/whoof` was unreachable
at audit; `noop-app/noop` is MIT and unused. A bibliography entry is not a
dependency, and recording the difference is what keeps the attribution above
accurate rather than over-broad.

**Attribution intent.** Attribution is given here as intellectual honesty rather
than as a license obligation, since none of these sources imposes one. Where a
repository is no longer reachable, it is cited by name and, where known, pinned
commit, with its unavailability recorded rather than silently dropped.
