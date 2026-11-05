"""Счётчик видеопотока: всё считается на лету, поэтому час потока не занимает память.

Сети здесь нет: на вход идут пакеты RTP одной видеодорожки. Время меряется двумя часами сразу.
Длительность, частота и битрейт — по меткам RTP, то есть по часам камеры: они не зависят от
того, как компьютер стенда разбирает очередь. Джиттер и задержка — по времени прихода. По нему же
считается вторая частота: метки прошивка может ставить по шаблону, тогда они покажут заявленное.
"""

from collections import Counter
from dataclasses import dataclass, field

from qa_tool.checks.onvif.replay import is_keyframe
from qa_tool.drivers.camera.rtsp import RtpPacket

SEQUENCE_SPACE = 1 << 16
TIMESTAMP_SPACE = 1 << 32
JITTER_GAIN = 16  # RFC 3550 §6.4.1
DROP_FACTOR = 2  # промежуток вдвое длиннее обычного — кадр пропущен
SLICE_S = 60
SECONDS_IN_HOUR = 3600
# Камера с кэшем группы кадров отдаёт его пачкой сразу после PLAY: это не джиттер сети и не темп
WARMUP_S = 3.0
MAX_B_RUN = 16  # предел H.264 и H.265 на практике; длинная серия назад — это сброс меток камеры


@dataclass
class Tally:
    """Счёт за весь поток или за одну минуту."""

    packets: int = 0
    lost: int = 0
    reordered: int = 0
    payload_bytes: int = 0
    frames: int = 0
    keyframes: int = 0
    ticks: int = 0  # сумма промежутков между кадрами в единицах метки RTP
    intervals: int = 0  # сколько промежутков: кадры B в ticks не входят, а в частоту входят
    b_run: int = 0  # самая длинная серия кадров B: на столько кадров законно шагают метки
    time_jumps: int = 0  # сколько раз метки отпрыгнули назад дальше кадров B
    steps: Counter = field(default_factory=Counter)  # промежуток, мс → сколько раз
    min_delay_s: float | None = None

    def stats(self, clock_rate: int) -> dict:
        """Итог счёта.

        Пропуски с кадрами B считаются приближённо: выпавший кадр B меток вперёд не двигает и
        виден только в частоте. Выпавшая группа кадров видна, поэтому строка не проходит.

        Args:
            clock_rate: частота меток RTP, Гц.
        """
        seconds = self.ticks / clock_rate
        usual = self.steps.most_common(1)[0][0] if self.steps else 0
        legal, limit = 1, usual * DROP_FACTOR
        if self.b_run and self.intervals:
            # с кадрами B метка вперёд шагает через серию: обычный шаг берётся средний за кадр,
            # а он сам растёт от пропусков, поэтому порог на кадр дальше законного шага
            usual, legal = seconds * 1000 / self.intervals, self.b_run + 1
            limit = usual * (legal + 1)
        dropped = (
            sum(
                n * max(round(gap / usual) - legal, 1)
                for gap, n in self.steps.items()
                if gap >= limit
            )
            if usual
            else 0
        )
        sent = self.packets + self.lost
        shown = self.frames + dropped
        return {
            "seconds": round(seconds, 3),
            "packets": self.packets,
            "lost": self.lost,
            "loss_pct": round(100 * self.lost / sent, 3) if sent else 0.0,
            "reordered": self.reordered,
            "frames": self.frames,
            "keyframes": self.keyframes,
            "fps": round(self.intervals / seconds, 2) if seconds else 0.0,
            "kbps": round(self.payload_bytes * 8 / seconds / 1000, 1) if seconds else 0.0,
            "dropped": dropped,
            "drop_pct": round(100 * dropped / shown, 3) if shown else 0.0,
            "longest_gap_s": round(max(self.steps, default=0) / 1000, 3),
            "time_jumps": self.time_jumps,
        }


class StreamMeter:
    """Копит счёт одной видеодорожки; кормить `add`, итог — `stats` и `minutes`.

    Args:
        encoding: кодек из SDP, нужен для поиска ключевых кадров.
        clock_rate: частота меток RTP, Гц.
        slice_s: длина среза таблицы, с.
        warmup_s: столько секунд от первого пакета не идёт в джиттер и частоту по приходу.
    """

    def __init__(
        self,
        encoding: str,
        clock_rate: int = 90_000,
        slice_s: float = SLICE_S,
        warmup_s: float = WARMUP_S,
    ):
        self.encoding = encoding
        self.clock_rate = clock_rate
        self.slice_s = slice_s
        self.warmup_s = warmup_s
        self.total = Tally()
        self.slices: list[Tally] = []
        self.jitter_s = 0.0
        self.max_jitter_s = 0.0
        self._jitter_sum = 0.0
        self._jitter_count = 0
        self.gops: Counter = Counter()
        self._next_sequence: int | None = None
        self._frame_timestamp: int | None = None
        self._timeline: int | None = None  # самая поздняя метка: кадры B идут позади неё
        self._b_run = 0
        self._frame_key = False
        self._frames_since_key: int | None = None
        self._first_arrival: float | None = None
        self._steady_from: float | None = None
        self._steady_to = 0.0
        self._steady_frames = 0
        self._elapsed_ticks = 0
        self._previous_transit: float | None = None
        self._transit_shift = 0.0
        self._rebase = False

    def reconnected(self) -> None:
        """Новая сессия потока: номера и метки у неё свои, это не потери и не рост задержки."""
        self._next_sequence = None
        self._frame_timestamp = None
        self._timeline = None
        self._b_run = 0
        self._frames_since_key = None
        self._steady_from = None
        self._steady_frames = 0
        self._rebase = True

    def _tallies(self, packet: RtpPacket) -> tuple[Tally, Tally]:
        index = int((packet.arrived - self._first_arrival) // self.slice_s)
        while len(self.slices) <= index:
            self.slices.append(Tally())
        return self.total, self.slices[index]

    def add(self, packet: RtpPacket) -> None:
        """Учесть пакет; битый (короче заголовка RTP) пропускается."""
        if packet.sequence < 0:
            return
        if self._first_arrival is None:
            self._first_arrival = packet.arrived
        tallies = self._tallies(packet)
        if not self._count_sequence(packet, tallies):
            return
        for tally in tallies:
            tally.packets += 1
            tally.payload_bytes += len(packet.payload)
        self._count_frame(packet, tallies)

    def _count_sequence(self, packet: RtpPacket, tallies: tuple[Tally, Tally]) -> bool:
        """Учесть номер пакета; False — пакет опоздал и его место уже посчитано потерянным."""
        if self._next_sequence is None:
            self._next_sequence = packet.sequence
        gap = (packet.sequence - self._next_sequence) % SEQUENCE_SPACE
        if gap >= SEQUENCE_SPACE // 2:  # номер из прошлого: UDP поменял пакеты местами
            for tally in tallies:
                tally.reordered += 1
                tally.lost = max(tally.lost - 1, 0)
                tally.packets += 1
                tally.payload_bytes += len(packet.payload)
            return False
        for tally in tallies:
            tally.lost += gap
        self._next_sequence = (packet.sequence + 1) % SEQUENCE_SPACE
        return True

    def _count_frame(self, packet: RtpPacket, tallies: tuple[Tally, Tally]) -> None:
        key = is_keyframe(self.encoding, packet.payload)
        if packet.timestamp == self._frame_timestamp:
            if key:
                self._mark_key(tallies)
            return
        self._frame_timestamp = packet.timestamp
        self._frame_key = False
        if self._frames_since_key is not None:
            self._frames_since_key += 1
        step = None
        if self._timeline is not None:
            step = (packet.timestamp - self._timeline) % TIMESTAMP_SPACE
        for tally in tallies:
            tally.frames += 1
            tally.intervals += step is not None
        if step is not None and step >= TIMESTAMP_SPACE // 2:
            self._count_backward(packet, tallies)
        else:
            self._move_timeline(packet, step, tallies)
        self._count_arrival(packet)
        if key:
            self._mark_key(tallies)

    def _count_backward(self, packet: RtpPacket, tallies: tuple[Tally, Tally]) -> None:
        """Кадр показывается раньше уже пришедшего: это кадр B или сброс меток камеры."""
        self._b_run += 1
        if self._b_run <= MAX_B_RUN:
            return
        self._b_run = 0
        self._timeline = packet.timestamp
        self._rebase = True
        for tally in tallies:
            tally.time_jumps += 1

    def _move_timeline(
        self, packet: RtpPacket, step: int | None, tallies: tuple[Tally, Tally]
    ) -> None:
        self._timeline = packet.timestamp
        for tally in tallies:
            tally.b_run = max(tally.b_run, self._b_run)
        self._b_run = 0
        if step is not None:
            self._elapsed_ticks += step
            for tally in tallies:
                tally.ticks += step
                tally.steps[round(step * 1000 / self.clock_rate)] += 1
        self._count_timing(packet, tallies)

    def _warmed_up(self, packet: RtpPacket) -> bool:
        return packet.arrived - self._first_arrival >= self.warmup_s

    def _count_arrival(self, packet: RtpPacket) -> None:
        if not self._warmed_up(packet):
            return
        if self._steady_from is None:
            self._steady_from = packet.arrived
        else:
            self._steady_frames += 1
        self._steady_to = packet.arrived

    def _mark_key(self, tallies: tuple[Tally, Tally]) -> None:
        if self._frame_key:
            return
        self._frame_key = True
        for tally in tallies:
            tally.keyframes += 1
        if self._frames_since_key is not None:
            self.gops[self._frames_since_key] += 1
        self._frames_since_key = 0

    def _count_timing(self, packet: RtpPacket, tallies: tuple[Tally, Tally]) -> None:
        """Джиттер и задержка — по первому пакету кадра.

        Пакеты одного кадра имеют одну метку и уходят пачкой: посчитанные по RFC 3550 каждый,
        они дали бы «джиттер» от размера кадра, а не от сети.
        """
        transit = packet.arrived - self._elapsed_ticks / self.clock_rate - self._transit_shift
        if self._rebase:  # время простоя в метки не попало: задержка продолжается с прежней
            self._rebase = False
            if self._previous_transit is not None:
                self._transit_shift += transit - self._previous_transit
                transit = self._previous_transit
        if self._previous_transit is not None:
            change = abs(transit - self._previous_transit)
            self.jitter_s += (change - self.jitter_s) / JITTER_GAIN
            if self._warmed_up(packet):
                self.max_jitter_s = max(self.max_jitter_s, self.jitter_s)
                self._jitter_sum += self.jitter_s
                self._jitter_count += 1
        self._previous_transit = transit
        for tally in tallies:
            if tally.min_delay_s is None or transit < tally.min_delay_s:
                tally.min_delay_s = transit

    @property
    def gop(self) -> int:
        """Самая частая длина группы кадров; 0 — полной группы не было."""
        return self.gops.most_common(1)[0][0] if self.gops else 0

    @property
    def delay_growth_s(self) -> float:
        """На сколько выросла задержка от первой минуты к последней.

        Берётся наименьшая задержка за минуту: она не зависит от всплесков. Рост значит, что
        копится очередь или что часы камеры расходятся с часами стенда; какое из двух, решает
        порог скорости роста в проверке.
        """
        delays = [s.min_delay_s for s in self.slices if s.min_delay_s is not None]
        return round(delays[-1] - delays[0], 3) if len(delays) > 1 else 0.0

    def worst_growth(self, window_s: float = SECONDS_IN_HOUR) -> tuple[float, float]:
        """Самый большой рост задержки внутри окна: поздняя очередь не тонет в среднем за сутки.

        Args:
            window_s: длина окна, с.

        Returns:
            Рост, с, и длина окна, с: на коротком потоке окно равно потоку.
        """
        delays = [s.min_delay_s for s in self.slices if s.min_delay_s is not None]
        reach = max(round(window_s / self.slice_s), 1)
        worst = 0.0
        for start, delay in enumerate(delays):
            for later in delays[start + 1 : start + 1 + reach]:
                worst = max(worst, later - delay)
        return round(worst, 3), min(window_s, max(len(delays) - 1, 1) * self.slice_s)

    @property
    def arrival_fps(self) -> float:
        """Частота по времени прихода, без разгона; 0 — поток короче разгона."""
        seconds = self._steady_to - (self._steady_from or 0.0)
        return round(self._steady_frames / seconds, 2) if self._steady_frames and seconds else 0.0

    def stats(self) -> dict:
        """Итог по всему потоку."""
        worst, window_s = self.worst_growth()
        mean = self._jitter_sum / self._jitter_count if self._jitter_count else 0.0
        return {
            **self.total.stats(self.clock_rate),
            "arrival_fps": self.arrival_fps,
            "gop": self.gop,
            "jitter_ms": round(self.max_jitter_s * 1000, 1),
            "jitter_mean_ms": round(mean * 1000, 1),
            "delay_growth_s": self.delay_growth_s,
            "delay_worst_s": worst,
            "delay_window_s": window_s,
        }

    def minutes(self) -> list[dict]:
        """Итоги по минутам — для таблицы стресс-теста."""
        return [s.stats(self.clock_rate) for s in self.slices]
