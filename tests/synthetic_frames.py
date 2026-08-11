"""Synthetic CRC-valid Gen4 frames for the public, data-free test suite."""

import zlib

from whoop_local.capture.proprietary_protocol import (
    EVENT_BATTERY_LEVEL,
    FRAME_START,
    PACKET_TYPE_EVENT,
    PACKET_TYPE_REALTIME_DATA,
    crc8,
)


def frame(payload: bytes) -> bytes:
    declared_len = len(payload) + 4
    header = bytes([FRAME_START]) + declared_len.to_bytes(2, "little")
    header += bytes([crc8(header[1:3])])
    return header + payload + (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")


def realtime_frame(hr_bpm: int, rr_values_ms: tuple[int, ...] = ()) -> bytes:
    if len(rr_values_ms) > 4:
        raise ValueError("the realtime payload has at most four RR slots")
    payload = bytearray(20)
    payload[0] = PACKET_TYPE_REALTIME_DATA
    payload[1] = 2
    payload[2:6] = (1234).to_bytes(4, "little")
    payload[8] = hr_bpm
    payload[9] = len(rr_values_ms)
    for index, value in enumerate(rr_values_ms):
        offset = 10 + index * 2
        payload[offset : offset + 2] = value.to_bytes(2, "little")
    payload[18] = 1
    payload[19] = 1
    return frame(bytes(payload))


def battery_event(raw_value: int) -> bytes:
    payload = bytearray(20)
    payload[0] = PACKET_TYPE_EVENT
    payload[2] = EVENT_BATTERY_LEVEL
    payload[13:15] = raw_value.to_bytes(2, "little")
    return frame(bytes(payload))


def unrelated_event() -> bytes:
    payload = bytearray(20)
    payload[0] = PACKET_TYPE_EVENT
    payload[2] = 99
    return frame(bytes(payload))
