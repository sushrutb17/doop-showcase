from tests.synthetic_frames import battery_event, realtime_frame, unrelated_event
from whoop_local.capture.proprietary_protocol import (
    build_command,
    crc8,
    parse_battery_raw,
    parse_frame,
    parse_realtime_hr,
    parse_realtime_rr,
    payload_crc32,
)


def test_crc8_known_vector():
    # CRC-8/SMBUS-style, poly 0x07, init 0, no reflection: single zero byte -> 0
    assert crc8(b"\x00") == 0
    # Non-trivial vector computed by the same bit-by-bit algorithm by hand for b"\x01"
    assert crc8(b"\x01") == 0x07


def test_build_command_round_trips_through_parse_frame():
    frame = build_command(sequence=1, command=3)
    parsed = parse_frame(frame)

    assert parsed.header_crc_valid
    assert parsed.payload_crc_valid
    assert parsed.packet_type == 35  # PACKET_TYPE_COMMAND
    assert parsed.sequence == 1
    assert parsed.command_or_event == 3


def test_build_command_with_data_round_trips():
    frame = build_command(sequence=42, command=145, data=b"\x01\x02\x03")
    parsed = parse_frame(frame)

    assert parsed.header_crc_valid
    assert parsed.payload_crc_valid
    assert parsed.sequence == 42
    assert parsed.command_or_event == 145
    assert parsed.payload[3:] == b"\x01\x02\x03"


def test_corrupted_payload_fails_crc_check():
    frame = bytearray(build_command(sequence=1, command=3))
    frame[-1] ^= 0xFF  # flip a bit in the trailing CRC-32
    parsed = parse_frame(bytes(frame))

    assert parsed.header_crc_valid  # header untouched
    assert not parsed.payload_crc_valid


def test_payload_crc32_matches_standard_ieee_crc32():
    # zlib.crc32 is the reference implementation of standard IEEE 802.3 CRC-32.
    import zlib

    data = b"hello whoop"
    assert payload_crc32(data) == zlib.crc32(data)


def test_parse_realtime_hr_on_synthetic_frames():
    # Even 10-BPM steps are deliberately generated to make byte round-tripping
    # obvious; they are not a retained physiological sequence.
    expected_bpm = list(range(60, 101, 10))

    for expected in expected_bpm:
        parsed = parse_frame(realtime_frame(expected))
        assert parsed.header_crc_valid
        assert parsed.payload_crc_valid
        assert parse_realtime_hr(parsed.payload) == expected


def test_parse_realtime_hr_rejects_wrong_shape():
    assert parse_realtime_hr(b"\x00" * 20) is None  # wrong packet_type
    assert parse_realtime_hr(b"\x28\x03" + b"\x00" * 18) is None  # wrong subtype
    assert parse_realtime_hr(b"\x28\x02\x00") is None  # too short


def test_parse_realtime_rr_on_synthetic_frames():
    cases = [
        ((), []),
        ((800,), [800.0]),
        ((800, 810), [800.0, 810.0]),
        ((800, 810, 820), [800.0, 810.0, 820.0]),
        ((800, 810, 820, 830), [800.0, 810.0, 820.0, 830.0]),
    ]
    for rr_values, expected_rr in cases:
        parsed = parse_frame(realtime_frame(72, rr_values))
        assert parsed.header_crc_valid
        assert parsed.payload_crc_valid
        assert parse_realtime_rr(parsed.payload) == expected_rr


def test_parse_realtime_rr_rejects_wrong_shape():
    assert parse_realtime_rr(b"\x00" * 20) == []
    assert parse_realtime_rr(b"\x28\x02\x00") == []


def test_parse_battery_raw_on_synthetic_samples():
    for expected_raw in (600, 800):
        parsed = parse_frame(battery_event(expected_raw))
        assert parsed.header_crc_valid
        assert parsed.payload_crc_valid
        assert parse_battery_raw(parsed.payload) == expected_raw


def test_parse_battery_raw_rejects_non_battery_events():
    parsed = parse_frame(unrelated_event())
    assert parsed.payload_crc_valid
    assert parse_battery_raw(parsed.payload) is None
    assert parse_battery_raw(b"\x00" * 20) is None  # wrong packet_type entirely
