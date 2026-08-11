"""WHOOP 4.0 (Gen4) proprietary GATT protocol — command framing + CRCs.

UUID mapping and frame structure sourced from the OpenWhoop community RE
project (bWanShiTong/openwhoop), cross-referenced against our own Phase 0
GATT scan — the two match exactly for Gen4. See ``CASE_STUDY.md`` and
``THIRD_PARTY_NOTICES.md`` for the confidence assessment and provenance.

Gen4 frame layout (4-byte header, distinct from newer WHOOP generations'
8-byte header):

    [0]     0xAA (FRAME_START)
    [1:3]   uint16 LE  -- declared_len = len(payload) + 4 (trailing CRC-32)
    [3]     uint8      -- CRC-8 of bytes [1:3] (the length field)
    [4:...] payload     -- [packet_type, sequence, command/event, ...data]
    [-4:]   uint32 LE  -- standard CRC-32 (IEEE 802.3) of the payload

The realtime toggle, data-range query, and history-send command numbers were
verified against this project's Gen4 hardware (D16/D27). The complete padded
history handshake, RTC format, metadata markers, and ACK cursor semantics are
independently corroborated by OpenStrap's MIT-licensed Gen4 implementation
(D69) and accepted on one owned Gen4. The offload path remains deliberately
opt-in and experimental because firmware or hardware variants may differ.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

# -- GATT UUIDs (confirmed against our own Phase 0 scan) --------------------

SERVICE_UUID = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
CHAR_COMMAND_TO_STRAP = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"  # write
CHAR_COMMAND_FROM_STRAP = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"  # notify (responses)
CHAR_EVENTS_FROM_STRAP = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"  # notify (events)
CHAR_DATA_FROM_STRAP = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"  # notify (sensor data)
CHAR_MEMFAULT = "61080007-8d6d-82b8-614a-1c8cb0f8dcc6"  # notify (diagnostics)

ALL_NOTIFY_UUIDS = (
    CHAR_COMMAND_FROM_STRAP,
    CHAR_EVENTS_FROM_STRAP,
    CHAR_DATA_FROM_STRAP,
    CHAR_MEMFAULT,
)

# -- Frame constants ----------------------------------------------------------

FRAME_START = 0xAA
PACKET_TYPE_COMMAND = 35
PACKET_TYPE_COMMAND_RESPONSE = 36
PACKET_TYPE_REALTIME_DATA = 40
PACKET_TYPE_HISTORICAL_DATA = 47
PACKET_TYPE_EVENT = 48
PACKET_TYPE_METADATA = 49

# Command numbers corroborated for Gen4; see D16/D27/D69 for confidence levels.
COMMAND_TOGGLE_REALTIME_HR = 3
COMMAND_SET_CLOCK = 10
COMMAND_GET_CLOCK = 11
# These three values are verified only as ordered steps in the complete
# hardware-accepted Gen4 history initialization sequence (D71). Their old
# community-source names claimed semantics this project did not independently
# establish, so retain the proven wire values under neutral names (D90).
COMMAND_HISTORY_INIT_STEP_1 = 35
COMMAND_HISTORY_INIT_STEP_2 = 76
COMMAND_HISTORY_INIT_STEP_4 = 67

# Phase 4 commands and transfer state machine. Commands 34/22 were directly
# observed on our hardware; the complete flow is corroborated by OpenStrap.
COMMAND_GET_DATA_RANGE = 34  # ReadOnly risk gate — query available history, no transfer
COMMAND_SEND_HISTORICAL_DATA = 22  # UserVisibleStateChange — triggers actual transfer
COMMAND_HISTORICAL_DATA_RESULT = 23  # ack on HistoryEnd
COMMAND_ABORT_HISTORICAL_TRANSMITS = 20  # cancel/retry after idle timeout

# Historical-transfer metadata subtypes (payload[2] on packet_type 49).
HISTORY_START = 1
HISTORY_END = 2
HISTORY_COMPLETE = 3
HISTORY_TOKEN_OFFSET = 13
HISTORY_TOKEN_LEN = 8


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, non-reflected, init 0 — matches the Gen4 header check."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def payload_crc32(payload: bytes) -> int:
    """Standard IEEE 802.3 CRC-32 (matches Rust's crc32fast::hash)."""
    return zlib.crc32(payload) & 0xFFFFFFFF


def build_command(sequence: int, command: int, data: bytes = b"") -> bytes:
    """Build a Gen4 command frame ready to write to CHAR_COMMAND_TO_STRAP."""
    payload = bytes([PACKET_TYPE_COMMAND, sequence, command]) + data
    declared_len = len(payload) + 4
    header = bytes([FRAME_START]) + declared_len.to_bytes(2, "little")
    header = header + bytes([crc8(header[1:3])])
    crc = payload_crc32(payload).to_bytes(4, "little")
    return header + payload + crc


def build_padded_command(sequence: int, command: int, data: bytes = b"\x00") -> bytes:
    """Build the 4-byte-aligned Gen4 command form used by history sync.

    The live toggle was empirically proven with the older unpadded helper above,
    so that helper intentionally remains byte-for-byte stable.  WHOOP's history
    INIT, RTC, and batch-ACK messages use an inner payload padded to a 4-byte
    boundary before the length and CRC-32 are calculated.
    """
    payload = bytes([PACKET_TYPE_COMMAND, sequence & 0xFF, command & 0xFF]) + data
    payload += b"\x00" * ((-len(payload)) % 4)
    declared_len = len(payload) + 4
    header = bytes([FRAME_START]) + declared_len.to_bytes(2, "little")
    header += bytes([crc8(header[1:3])])
    return header + payload + payload_crc32(payload).to_bytes(4, "little")


def build_set_clock(sequence: int, now_ms: int) -> bytes:
    """Build WHOOP 4's 8-byte RTC payload (seconds + 1/32768s fraction)."""
    seconds, millis = divmod(now_ms, 1000)
    subseconds = millis * 32768 // 1000
    data = seconds.to_bytes(4, "little") + subseconds.to_bytes(4, "little")
    return build_padded_command(sequence, COMMAND_SET_CLOCK, data)


def build_history_init_commands() -> tuple[bytes, ...]:
    """Hardware-verified five-command Gen4 history initialization sequence."""
    return (
        build_padded_command(0, COMMAND_HISTORY_INIT_STEP_1, b"\x00"),
        build_padded_command(1, COMMAND_HISTORY_INIT_STEP_2, b"\x00"),
        build_padded_command(2, COMMAND_GET_DATA_RANGE, b"\x00"),
        build_padded_command(3, COMMAND_HISTORY_INIT_STEP_4, b"\x01"),
        build_padded_command(4, COMMAND_SEND_HISTORICAL_DATA, b"\x00"),
    )


def build_history_ack(sequence: int, token: bytes) -> bytes:
    """Acknowledge one durable history chunk by echoing its exact 8-byte cursor."""
    if len(token) != HISTORY_TOKEN_LEN:
        raise ValueError(f"history token must be {HISTORY_TOKEN_LEN} bytes")
    return build_padded_command(
        sequence,
        COMMAND_HISTORICAL_DATA_RESULT,
        b"\x01" + token,
    )


@dataclass
class ParsedFrame:
    header_crc_valid: bool
    payload_crc_valid: bool
    packet_type: int | None
    sequence: int | None
    command_or_event: int | None
    payload: bytes


@dataclass(frozen=True)
class HistoryMetadata:
    subtype: int
    expected_packet_count: int | None = None
    token: bytes | None = None
    batch_id: int | None = None


@dataclass(frozen=True)
class HistoricalRecord:
    version: int
    counter: int
    device_ts_utc: int
    hr_bpm: int
    rr_values_ms: tuple[float, ...]


def parse_frame(frame: bytes) -> ParsedFrame:
    """Best-effort parse of a Gen4 frame received on a notify characteristic."""
    if len(frame) < 4 or frame[0] != FRAME_START:
        raise ValueError(f"Not a Gen4 frame (no 0xAA start byte): {frame.hex()}")

    declared_len = int.from_bytes(frame[1:3], "little")
    header_crc_valid = crc8(frame[1:3]) == frame[3]

    payload_end = 4 + max(declared_len - 4, 0)
    payload = frame[4:payload_end]
    payload_crc_valid = False
    if len(frame) >= payload_end + 4:
        expected_crc = int.from_bytes(frame[payload_end : payload_end + 4], "little")
        payload_crc_valid = payload_crc32(payload) == expected_crc

    packet_type = payload[0] if len(payload) > 0 else None
    sequence = payload[1] if len(payload) > 1 else None
    command_or_event = payload[2] if len(payload) > 2 else None

    return ParsedFrame(
        header_crc_valid=header_crc_valid,
        payload_crc_valid=payload_crc_valid,
        packet_type=packet_type,
        sequence=sequence,
        command_or_event=command_or_event,
        payload=payload,
    )


class FrameReassembler:
    """Length-based Gen4 frame reassembly across arbitrary BLE notifications."""

    def __init__(self, max_frame_len: int = 4096, max_buffer_len: int = 8192):
        self._buffer = bytearray()
        self.max_frame_len = max_frame_len
        self.max_buffer_len = max_buffer_len

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while len(self._buffer) >= 8:
            if self._buffer[0] != FRAME_START:
                try:
                    next_start = self._buffer.index(FRAME_START, 1)
                except ValueError:
                    self._buffer.clear()
                    break
                del self._buffer[:next_start]
                continue
            declared_len = int.from_bytes(self._buffer[1:3], "little")
            total_len = 4 + declared_len
            if declared_len < 4 or total_len > self.max_frame_len:
                del self._buffer[0]
                continue
            if len(self._buffer) < total_len:
                break
            frames.append(bytes(self._buffer[:total_len]))
            del self._buffer[:total_len]
            while self._buffer and self._buffer[0] == 0:
                del self._buffer[0]
        if len(self._buffer) > self.max_buffer_len:
            self._buffer.clear()
        return frames


def parse_history_metadata(payload: bytes) -> HistoryMetadata | None:
    """Parse HISTORY_START/END/COMPLETE metadata and the END continuation token."""
    if len(payload) < 3 or payload[0] != PACKET_TYPE_METADATA:
        return None
    subtype = payload[2]
    expected = (
        int.from_bytes(payload[9:13], "little")
        if subtype == HISTORY_END and len(payload) >= 13
        else None
    )
    token = None
    batch_id = None
    if subtype == HISTORY_END and len(payload) >= HISTORY_TOKEN_OFFSET + HISTORY_TOKEN_LEN:
        token = payload[HISTORY_TOKEN_OFFSET : HISTORY_TOKEN_OFFSET + HISTORY_TOKEN_LEN]
        batch_id = int.from_bytes(token[4:8], "little")
    return HistoryMetadata(subtype, expected, token, batch_id)


def parse_historical_record(payload: bytes) -> HistoricalRecord | None:
    """Decode the hardware-validated WHOOP 4 v12/v24 one-second record layout.

    Other versions remain preserved verbatim in raw/history tables until their
    layouts are independently validated for this physical band.
    """
    if len(payload) < 72 or payload[0] != PACKET_TYPE_HISTORICAL_DATA:
        return None
    version = payload[1]
    if version not in (12, 24):
        return None
    seconds = int.from_bytes(payload[7:11], "little")
    subseconds = int.from_bytes(payload[11:13], "little")
    device_ts_utc = seconds * 1000 + subseconds * 1000 // 32768
    hr_bpm = payload[17]
    rr_count = min(payload[18], 4)
    rr_values: list[float] = []
    for index in range(rr_count):
        offset = 19 + index * 2
        if offset + 2 > len(payload):
            break
        value = int.from_bytes(payload[offset : offset + 2], "little", signed=True)
        if value > 0:
            rr_values.append(float(value))
    return HistoricalRecord(
        version=version,
        counter=int.from_bytes(payload[3:7], "little"),
        device_ts_utc=device_ts_utc,
        hr_bpm=hr_bpm,
        rr_values_ms=tuple(rr_values),
    )


# -- Gen4 realtime HR/RR packet (empirically decoded during hardware validation) --
#
# Received on CHAR_DATA_FROM_STRAP after writing COMMAND_TOGGLE_REALTIME_HR.
# 20-byte payload, packet_type=PACKET_TYPE_REALTIME_DATA(40), payload[1]==2.
#
# HR: confirmed by capturing ~20 live samples and finding payload[8] varies
# smoothly in a physiological BPM range while other bytes looked constant.
#
# RR: the initial short capture saw bytes 9-17 as all-zero and left them
# undecoded. A later, longer validation capture revealed real variation:
# payload[9] is an RR-count flag (mirrors
# the standard BLE HR spec's RR-interval list convention), and when non-zero,
# uint16 LE RR intervals in ms follow. The short capture only saw counts 0-2
# and left bytes 14-17 marked "constant zero, SpO2/temp candidates" — later
# disproven by a re-scan across the retained validation corpus. Counts 3 and 4
# occur, and payload[14:16] / payload[16:18] are RR slots 3 and 4. Their values
# stayed inside the configured validation range and were contiguous with slots
# 1-2 in the same packet. SpO2/skin temp
# are therefore NOT in this payload at all.
#
# Remaining payload map (full retained-corpus scan):
#   [2:6]   uint32 LE counter ticking at ~1.001/s — device uptime seconds.
#   [6:8]   varies every packet, undecoded (sub-second timer candidate).
#   [18]    constant 0x01 across all packets.
#   [19]    state byte: long stable runs of 1/2/5; 0x80 appears around
#           charging/off-wrist windows. Meaning undecoded, nothing reads it.
#
# This does NOT match the (untrusted) whoop-reader repo's claimed 96-byte/
# uint16*100 HR layout — our own empirical capture is the source of truth.
REALTIME_HR_PAYLOAD_LEN = 20
REALTIME_HR_SUBTYPE = 2  # payload[1] for this packet shape
REALTIME_HR_BPM_OFFSET = 8
REALTIME_RR_COUNT_OFFSET = 9
REALTIME_RR_VALUE_OFFSETS = (10, 12, 14, 16)  # each a uint16 LE, used per REALTIME_RR_COUNT_OFFSET


def _is_realtime_hr_shape(payload: bytes) -> bool:
    return (
        len(payload) >= REALTIME_HR_PAYLOAD_LEN
        and payload[0] == PACKET_TYPE_REALTIME_DATA
        and payload[1] == REALTIME_HR_SUBTYPE
    )


def parse_realtime_hr(payload: bytes) -> int | None:
    """Extract heart rate (BPM) from a Gen4 realtime-data payload, if present."""
    if not _is_realtime_hr_shape(payload):
        return None
    return payload[REALTIME_HR_BPM_OFFSET]


def parse_realtime_rr(payload: bytes) -> list[float]:
    """Extract RR interval(s) in ms from a Gen4 realtime-data payload, if present."""
    if not _is_realtime_hr_shape(payload):
        return []
    count = payload[REALTIME_RR_COUNT_OFFSET]
    rr_values = []
    for i in range(min(count, len(REALTIME_RR_VALUE_OFFSETS))):
        offset = REALTIME_RR_VALUE_OFFSETS[i]
        rr_values.append(float(int.from_bytes(payload[offset : offset + 2], "little")))
    return rr_values


# -- Gen4 battery event (empirically decoded during D28 hardware validation) --
#
# Received on CHAR_EVENTS_FROM_STRAP, packet_type=PACKET_TYPE_EVENT(48),
# event=EVENT_BATTERY_LEVEL(3), arrives periodically on its own (no command
# needed). Decoded by correlating a multi-hour capture (raw packets already
# stored every event) against an observed battery-death incident:
# payload[13:15] (uint16 LE) declined to zero before the band stopped
# recording, then rose again once charging began. This is
# a real fuel-gauge counter, not a calibrated 0-100% value — WHOOP's
# voltage-to-percentage curve isn't public, so `battery_pct` in queries.py
# is self-calibrated against the highest raw value observed so far rather
# than an absolute manufacturer scale.
EVENT_BATTERY_LEVEL = 3
BATTERY_RAW_OFFSET = 13


def _is_battery_event(payload: bytes) -> bool:
    return (
        len(payload) >= BATTERY_RAW_OFFSET + 2
        and payload[0] == PACKET_TYPE_EVENT
        and len(payload) > 2
        and payload[2] == EVENT_BATTERY_LEVEL
    )


def parse_battery_raw(payload: bytes) -> int | None:
    """Extract the raw battery fuel-gauge counter from a Gen4 event payload, if present."""
    if not _is_battery_event(payload):
        return None
    return int.from_bytes(payload[BATTERY_RAW_OFFSET : BATTERY_RAW_OFFSET + 2], "little")
