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


EVEN_SKIPS = {n for n in range(400) if n % 5 == 4}  # 20 кадр/с из датчика на 25: 40, 40, 40, 80 мс


def test_camera_set_to_lower_frame_rate_has_no_dropped_frames():
    meter = StreamMeter("H264", declared_fps=20, fps_pct=10)  # так шлют камеры 06.11
    feed(meter, 400, skip=EVEN_SKIPS)
    assert meter.stats()["dropped"] == 0


def test_rate_close_to_sensor_rate_is_still_thinned_by_settings():
    meter = StreamMeter("H264", declared_fps=23, fps_pct=10)  # Beward: 23 кадр/с из 25
    feed(meter, 500, skip={n for n in range(500) if n % 25 in (6, 18)})
    assert meter.stats()["dropped"] == 0


def test_same_skips_without_lower_declared_rate_are_dropped_frames():
    for meter in (StreamMeter("H264", declared_fps=25, fps_pct=10), StreamMeter("H264")):
        feed(meter, 400, skip=EVEN_SKIPS)
        assert meter.stats()["dropped"] == 79  # последний пропуск в конце: шага после него нет


def test_longer_gap_on_top_of_thinned_frames_is_still_a_dropped_frame():
    meter = StreamMeter("H264", declared_fps=20, fps_pct=10)
    feed(meter, 400, skip=EVEN_SKIPS | {200, 201, 202})
    assert meter.stats()["dropped"] > 0


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


def test_jitter_grows_with_uneven_arrival():
    meter = StreamMeter("H264", warmup_s=0)
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


def feed_with_b_frames(meter, groups):
    """I P B B в порядке передачи, 25 кадров/с показа: метки B позади уже пришедшего P."""
    order = [0, 3, 1, 2]
    for group in range(groups):
        for place, shown in enumerate(order):
            n = group * 4 + place
            payload = IDR if place == 0 and group % 6 == 0 else SLICE
            meter.add(packet(n, (group * 4 + shown) * STEP, n * 0.04, payload))


def test_b_frames_count_as_frames():
    meter = StreamMeter("H264")
    feed_with_b_frames(meter, 25)
    stats = meter.stats()
    assert stats["frames"] == 100
    assert 24.5 < stats["fps"] < 25.5
    assert stats["gop"] == 24
    assert stats["dropped"] == 0


def test_frames_lost_among_b_frames_are_noticed():
    meter = StreamMeter("H264")
    feed_with_b_frames(meter, 25)
    start = 100 * STEP
    for n, shown in enumerate((12, 13, 14)):  # камера пропустила двенадцать кадров подряд
        meter.add(packet(100 + n, start + shown * STEP, (100 + n) * 0.04))
    assert meter.stats()["dropped"] > 0


def test_arrival_rate_shows_what_template_timestamps_hide():
    meter = StreamMeter("H264")
    for n in range(300):  # настоящие 22 кадра/с, метки по шаблону на 25
        meter.add(packet(n, n * STEP, n / 22))
    stats = meter.stats()
    assert stats["fps"] == 25.0
    assert 21.9 < stats["arrival_fps"] < 22.1


def test_short_stream_has_no_arrival_rate():
    meter = StreamMeter("H264")
    feed(meter, 25)
    assert meter.stats()["arrival_fps"] == 0.0


def test_burst_after_play_is_not_jitter():
    meter = StreamMeter("H264")
    for n in range(50):  # кэш группы кадров: 2 секунды видео за 50 мс
        meter.add(packet(n, n * STEP, n * 0.001))
    for n in range(50, 500):
        meter.add(packet(n, n * STEP, 0.05 + (n - 50) * 0.04))
    stats = meter.stats()
    assert stats["jitter_ms"] < 5
    assert stats["jitter_mean_ms"] <= stats["jitter_ms"]


def test_jitter_after_warmup_is_still_seen():
    meter = StreamMeter("H264")
    for n in range(300):
        late = 0.03 if n % 2 and n > 100 else 0.0
        meter.add(packet(n, n * STEP, n * 0.04 + late))
    assert meter.stats()["jitter_ms"] > 15


def test_lost_group_among_b_frames_is_noticed():
    meter = StreamMeter("H264")
    sent = 0
    for group in range(188):  # I P B B, каждая восьмая группа не выдана
        if group % 8 == 7:
            continue
        for shown in (0, 3, 1, 2):
            meter.add(packet(sent, (group * 4 + shown) * STEP, group * 0.16 + shown * 0.01))
            sent += 1
    stats = meter.stats()
    assert stats["dropped"] > 0
    assert stats["drop_pct"] > 0.1


def test_timestamps_jumping_back_are_not_b_frames():
    meter = StreamMeter("H264")
    feed(meter, 100, start_ts=1_000_000)
    feed(meter, 100, start_seq=100, start_ts=0)
    stats = meter.stats()
    assert stats["time_jumps"] == 1
    assert stats["frames"] == 200


def test_queue_late_in_a_long_run_is_seen_in_its_window():
    meter = StreamMeter("H264", slice_s=60)
    timestamp = 0
    for minute in range(480):  # восемь часов ровно, в последний час очередь растёт на 0,5 с/мин
        lag = 0.5 * (minute - 420) if minute >= 420 else 0.0
        for n in range(2):
            meter.add(packet(minute * 2 + n, timestamp, minute * 60.0 + n * 30.0 + lag))
            timestamp += 30 * 90_000
    worst, window = meter.worst_growth()
    assert 25 < worst <= 31
    assert window == 3600


def test_single_timestamp_back_does_not_hide_lost_frames():
    meter = StreamMeter("H264")
    sent = 0
    for n in range(800):
        if n % 13 == 12:
            continue  # пропущен кадр
        meter.add(packet(sent, n * STEP, n * 0.04))
        sent += 1
        if n == 100:  # одна случайная метка назад
            meter.add(packet(sent, (n - 1) * STEP + 100, n * 0.04 + 0.001))
            sent += 1
    assert meter.stats()["dropped"] > 40


def test_rare_b_frames_are_not_lost_frames():
    meter = StreamMeter("H264")
    sent = 0
    for n in range(0, 780, 26):  # в группе из 26 кадров один кадр B
        for shown in [*range(0, 24), 25, 24]:
            meter.add(packet(sent, (n + shown) * STEP, sent * 0.04))
            sent += 1
    assert meter.stats()["drop_pct"] < 0.1
