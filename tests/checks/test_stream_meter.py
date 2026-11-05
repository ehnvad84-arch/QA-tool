"""Счётчик потока: точные значения на заданных последовательностях пакетов."""

from qa_tool.checks.stream.meter import StreamMeter
from qa_tool.drivers.camera.rtsp import RtpPacket

IDR = b"\x65" + b"\x00" * 99  # NAL 5: ключевой кадр H.264
SLICE = b"\x41" + b"\x00" * 99  # NAL 1: обычный кадр
STEP = 3600  # 25 кадров/с при 90 кГц


def packet(sequence, timestamp, arrived, payload=SLICE):
    return RtpPacket(0, True, payload, timestamp % 2**32, b"", sequence % 2**16, arrived)


def feed(meter, frames, start_seq=0, start_ts=0, gop=25, skip=()):
    """`frames` кадров по одному пакету, 25 кадров/с; номера из `skip` не приходят."""
    for n in range(frames):
        if n in skip:
            continue
        payload = IDR if n % gop == 0 else SLICE
        meter.add(packet(start_seq + n, start_ts + n * STEP, n * 0.04, payload))


def test_clean_stream():
    meter = StreamMeter("H264")
    feed(meter, 101)
    stats = meter.stats()
    assert stats["packets"] == 101
    assert stats["lost"] == 0
    assert stats["frames"] == 101
    assert stats["keyframes"] == 5
    assert stats["seconds"] == 4.0
    assert stats["fps"] == 25.0
    assert stats["kbps"] == round(101 * 100 * 8 / 4 / 1000, 1)
    assert stats["gop"] == 25
    assert stats["dropped"] == 0
    assert stats["jitter_ms"] == 0.0


def test_lost_packets_are_counted_across_sequence_wrap():
    meter = StreamMeter("H264")
    feed(meter, 50, start_seq=65_520, skip={20, 21, 22})
    stats = meter.stats()
    assert stats["lost"] == 3
    assert stats["packets"] == 47
    assert stats["loss_pct"] == 6.0


def test_timestamp_wrap_does_not_break_duration():
    meter = StreamMeter("H264")
    feed(meter, 26, start_ts=2**32 - 10 * STEP)
    stats = meter.stats()
    assert stats["seconds"] == 1.0
    assert stats["fps"] == 25.0


def test_reordered_packet_is_not_a_loss():
    meter = StreamMeter("H264")
    meter.add(packet(1, 0, 0.0))
    meter.add(packet(3, 2 * STEP, 0.08))
    meter.add(packet(2, STEP, 0.09))
    meter.add(packet(4, 3 * STEP, 0.12))
    stats = meter.stats()
    assert stats["lost"] == 0
    assert stats["reordered"] == 1
    assert stats["packets"] == 4


def test_several_packets_of_one_frame_are_one_frame():
    meter = StreamMeter("H264")
    for n in range(10):
        for part in range(3):
            meter.add(packet(n * 3 + part, n * STEP, n * 0.04 + part * 0.001))
    stats = meter.stats()
    assert stats["packets"] == 30
    assert stats["frames"] == 10
    assert stats["jitter_ms"] == 0.0  # пачка пакетов одного кадра — не джиттер


def test_dropped_frames_from_timestamp_gaps():
    meter = StreamMeter("H264")
    shown = [*range(50), *range(53, 100)]  # три кадра камера не выдала
    for sequence, n in enumerate(shown):
        meter.add(packet(sequence, n * STEP, n * 0.04))
    stats = meter.stats()
    assert stats["lost"] == 0
    assert stats["dropped"] == 3
    assert stats["frames"] == 97
    assert stats["drop_pct"] == 3.0
    assert stats["longest_gap_s"] == 0.16


def test_declared_step_finds_a_stream_that_is_steadily_slow():
    meter = StreamMeter("H264")
    for n in range(50):
        meter.add(packet(n, n * 3 * STEP, n * 0.12))
    assert meter.stats()["dropped"] == 0  # сам по себе поток ровный
    assert meter.stats(step_ms=40)["dropped"] == 98  # а заявлено втрое чаще


def test_jitter_grows_with_uneven_arrival():
    meter = StreamMeter("H264")
    for n in range(100):
        meter.add(packet(n, n * STEP, n * 0.04 + (0.02 if n % 2 else 0)))
    assert 15 < meter.stats()["jitter_ms"] <= 20


def test_gop_is_the_most_common_length():
    meter = StreamMeter("H264")
    feed(meter, 121, gop=30)
    assert meter.stats()["gop"] == 30
    assert meter.stats()["keyframes"] == 5


def test_key_nal_in_later_packet_of_frame_is_counted_once():
    meter = StreamMeter("H264")
    meter.add(packet(0, 0, 0.0, b"\x67\x00"))  # SPS не ключевой кадр сам по себе
    meter.add(packet(1, 0, 0.001, IDR))
    meter.add(packet(2, 0, 0.002, IDR))
    meter.add(packet(3, STEP, 0.04))
    assert meter.stats()["keyframes"] == 1


def test_minutes_and_delay_growth():
    meter = StreamMeter("H264")
    for n in range(25 * 180):  # три минуты, задержка растёт на 1 с в минуту
        second = n * 0.04
        meter.add(packet(n, n * STEP, second + second / 60))
    minutes = meter.minutes()
    assert len(minutes) >= 3
    assert all(m["lost"] == 0 for m in minutes)
    assert 1.5 < meter.delay_growth_s < 3


def test_steady_delay_is_not_growth():
    meter = StreamMeter("H264")
    for n in range(25 * 130):
        meter.add(packet(n, n * STEP, n * 0.04 + (0.05 if n % 7 == 0 else 0)))
    assert abs(meter.delay_growth_s) < 0.01


def test_malformed_packet_is_ignored():
    meter = StreamMeter("H264")
    meter.add(packet(0, 0, 0.0))
    meter.add(RtpPacket(0, False, b"", sequence=-1, arrived=0.01))
    meter.add(packet(1, STEP, 0.04))
    stats = meter.stats()
    assert stats["packets"] == 2
    assert stats["lost"] == 0


def test_empty_meter():
    stats = StreamMeter("H264").stats()
    assert stats["packets"] == 0
    assert stats["fps"] == 0.0
    assert stats["gop"] == 0


def test_new_session_is_neither_loss_nor_delay_growth():
    meter = StreamMeter("H264", slice_s=1)
    feed(meter, 50)
    meter.reconnected()
    for n in range(50):  # новая сессия: другие номера и метки, простой 30 с
        meter.add(packet(40_000 + n, 7_000_000 + n * STEP, 32 + n * 0.04))
    stats = meter.stats()
    assert stats["lost"] == 0
    assert stats["packets"] == 100
    assert stats["delay_growth_s"] == 0.0
