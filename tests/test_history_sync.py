import asyncio
import sqlite3
import time
import zlib

from whoop_local.capture.history_sync import (
    HistoryOffloader,
    HistoryStore,
    _HistoryFrame,
    _TransportChunk,
)
from whoop_local.capture.proprietary_protocol import (
    CHAR_DATA_FROM_STRAP,
    COMMAND_HISTORICAL_DATA_RESULT,
    COMMAND_SEND_HISTORICAL_DATA,
    FRAME_START,
    HISTORY_COMPLETE,
    HISTORY_END,
    HISTORY_START,
    PACKET_TYPE_HISTORICAL_DATA,
    PACKET_TYPE_METADATA,
    FrameReassembler,
    build_history_ack,
    build_history_init_commands,
    build_set_clock,
    crc8,
    parse_frame,
    parse_historical_record,
    parse_history_metadata,
)
from whoop_local.db.schema import SCHEMA_VERSION, init_db


def _frame(payload: bytes) -> bytes:
    payload += b"\x00" * ((-len(payload)) % 4)
    declared = len(payload) + 4
    header = bytes([FRAME_START]) + declared.to_bytes(2, "little")
    header += bytes([crc8(header[1:3])])
    return header + payload + (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")


def _historical_frame(ts_seconds: int, *, counter: int = 7, hr: int = 70) -> bytes:
    payload = bytearray(89)
    payload[0] = PACKET_TYPE_HISTORICAL_DATA
    payload[1] = 24
    payload[3:7] = counter.to_bytes(4, "little")
    payload[7:11] = ts_seconds.to_bytes(4, "little")
    payload[11:13] = (16384).to_bytes(2, "little")  # half a second
    payload[17] = hr
    payload[18] = 2
    payload[19:21] = (800).to_bytes(2, "little", signed=True)
    payload[21:23] = (810).to_bytes(2, "little", signed=True)
    return _frame(bytes(payload))


def _metadata(subtype: int, token: bytes | None = None) -> bytes:
    payload = bytearray(21 if token is not None else 4)
    payload[0] = PACKET_TYPE_METADATA
    payload[2] = subtype
    if token is not None:
        payload[9:13] = (1).to_bytes(4, "little")
        payload[13:21] = token
    return _frame(bytes(payload))


def test_history_init_matches_hardware_verified_vectors():
    expected = [
        "aa0800a823002300ada86a2d",
        "aa0800a823014c00f2b5cdce",
        "aa0800a823022200824df537",
        "aa0800a823034301c54dd63d",
        "aa0800a823041600c7c25288",
    ]
    assert [frame.hex() for frame in build_history_init_commands()] == expected


def test_set_clock_uses_seconds_and_32768hz_fraction():
    parsed = parse_frame(build_set_clock(9, 1_735_689_600_500))
    assert parsed.header_crc_valid and parsed.payload_crc_valid
    assert int.from_bytes(parsed.payload[3:7], "little") == 1_735_689_600
    assert int.from_bytes(parsed.payload[7:11], "little") == 16_384


def test_metadata_token_is_echoed_verbatim_in_ack():
    token = bytes.fromhex("deadbeef11223344")
    marker = parse_history_metadata(parse_frame(_metadata(HISTORY_END, token)).payload)
    assert marker is not None
    assert marker.subtype == HISTORY_END
    assert marker.token == token
    ack = parse_frame(build_history_ack(5, marker.token))
    assert ack.command_or_event == COMMAND_HISTORICAL_DATA_RESULT
    assert ack.payload[3:12] == b"\x01" + token


def test_frame_reassembler_handles_splits_and_multiple_frames():
    first = build_history_init_commands()[0]
    second = build_history_init_commands()[1]
    reassembler = FrameReassembler()
    assert reassembler.feed(first[:3]) == []
    assert reassembler.feed(first[3:] + second) == [first, second]


def test_parse_v24_historical_record():
    raw = _historical_frame(1_735_689_600)
    record = parse_historical_record(parse_frame(raw).payload)
    assert record is not None
    assert record.version == 24
    assert record.counter == 7
    assert record.device_ts_utc == 1_735_689_600_500
    assert record.hr_bpm == 70
    assert record.rr_values_ms == (800.0, 810.0)


def test_store_is_raw_first_and_replay_safe(tmp_path):
    conn = init_db(tmp_path / "test.db")
    store = HistoryStore(conn, "mac")
    ts_seconds = int(time.time()) - 10
    raw = _historical_frame(ts_seconds)
    received = int(time.time() * 1000)
    transport = [_TransportChunk(received, CHAR_DATA_FROM_STRAP, raw)]
    record = _HistoryFrame(received, raw, parse_frame(raw).payload)

    assert store.persist_chunk(transport, [record]) == (1, 1, 2)
    assert store.persist_chunk(transport, [record]) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM raw_packets").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM historical_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0] == 2


def test_schema_v12_rr_table_migrates_to_history_dedup(tmp_path):
    db_path = tmp_path / "v12.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE rr_intervals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc INTEGER NOT NULL, "
        "device_ts_utc INTEGER, rr_ms REAL NOT NULL, source_host TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'hrm')"
    )
    legacy.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    legacy.execute("INSERT INTO meta VALUES ('schema_version', '12')")
    legacy.commit()
    legacy.close()

    conn = init_db(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(rr_intervals)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(rr_intervals)")}
    assert "rr_index" in columns
    assert "idx_rr_history_dedup" in indexes
    assert (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == SCHEMA_VERSION
    )


class _FakeClient:
    def __init__(self, frames: list[bytes]):
        self.callbacks = {}
        self.frames = frames
        self.writes: list[bytes] = []

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, _uuid, data, response=True):
        self.writes.append(bytes(data))
        parsed = parse_frame(bytes(data))
        if parsed.command_or_event == COMMAND_SEND_HISTORICAL_DATA:
            callback = self.callbacks[CHAR_DATA_FROM_STRAP]
            for frame in self.frames:
                callback(None, bytearray(frame))
        await asyncio.sleep(0)


def test_offloader_commits_then_acks_and_completes(tmp_path):
    conn = init_db(tmp_path / "test.db")
    token = bytes.fromhex("0102030405060708")
    frames = [
        _metadata(HISTORY_START),
        _historical_frame(int(time.time()) - 5),
        _metadata(HISTORY_END, token),
        _metadata(HISTORY_COMPLETE),
    ]
    client = _FakeClient(frames)

    report = asyncio.run(HistoryOffloader(conn, "mac", timeout_s=2).run(client))

    assert report.complete
    assert report.records_received == 1
    assert report.records_inserted == 1
    assert report.batches_acked == 1
    acknowledgements = [
        parse_frame(raw)
        for raw in client.writes
        if parse_frame(raw).command_or_event == COMMAND_HISTORICAL_DATA_RESULT
    ]
    assert len(acknowledgements) == 1
    assert acknowledgements[0].payload[4:12] == token
    assert conn.execute("SELECT COUNT(*) FROM historical_records").fetchone()[0] == 1
