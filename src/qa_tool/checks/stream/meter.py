"""Счётчик видеопотока: всё считается на лету, поэтому час потока не занимает память.

Сети здесь нет: на вход идут пакеты RTP одной видеодорожки. Время меряется двумя часами сразу.
Длительность, частота и битрейт — по меткам RTP, то есть по часам камеры: они не зависят от
того, как компьютер стенда разбирает очередь. Джиттер и задержка — по времени прихода.
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
    steps: Counter = field(default_factory=Counter)  # промежуток, мс → сколько раз
    min_delay_s: float | None = None

    def stats(self, clock_rate: int, step_ms: int | None = None) -> dict:
        """Итог счёта.

        Args:
            clock_rate: частота меток RTP, Гц.
            step_ms: заявленный промежуток между кадрами, мс; None — самый частый измеренный.
        """
        seconds = self.ticks / clock_rate
        usual = step_ms or (self.steps.most_common(1)[0][0] if self.steps else 0)
        limit = usual * DROP_FACTOR
        dropped = (
            sum(n * (round(gap / usual) - 1) for gap, n in self.steps.items() if gap >= limit)
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
            "fps": round(sum(self.steps.values()) / seconds, 2) if seconds else 0.0,
            "kbps": round(self.payload_bytes * 8 / seconds / 1000, 1) if seconds else 0.0,
            "dropped": dropped,
            "drop_pct": round(100 * dropped / shown, 3) if shown else 0.0,
            "longest_gap_s": round(max(self.steps, default=0) / 1000, 3),
        }


class StreamMeter:
    """Копит счёт одной видеодорожки; кормить `add`, итог — `stats` и `minutes`.

    Args:
        encoding: кодек из SDP, нужен для поиска ключевых кадров.
        clock_rate: частота меток RTP, Гц.
    """

    def __init__(self, encoding: str, clock_rate: int = 90_000):
        self.encoding = encoding
        self.clock_rate = clock_rate
        self.total = Tally()
        self.slices: list[Tally] = []
        self.jitter_s = 0.0
        self.max_jitter_s = 0.0
        self.gops: Counter = Counter()
        self._next_sequence: int | None = None
        self._frame_timestamp: int | None = None
        self._frame_key = False
        self._frames_since_key: int | None = None
        self._first_arrival: float | None = None
        self._last_arrival = 0.0
        self._elapsed_ticks = 0
        self._previous_transit: float | None = None

    def _tallies(self, packet: RtpPacket) -> tuple[Tally, Tally]:
        index = int((packet.arrived - self._first_arrival) // SLICE_S)
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
        step = None
        if self._frame_timestamp is not None:
            step = (packet.timestamp - self._frame_timestamp) % TIMESTAMP_SPACE
            if step >= TIMESTAMP_SPACE // 2:
                return  # метка из прошлого: кадр B или опоздавший пакет
        self._frame_timestamp = packet.timestamp
        self._frame_key = False
        if self._frames_since_key is not None:
            self._frames_since_key += 1
        for tally in tallies:
            tally.frames += 1
        if step is not None:
            self._elapsed_ticks += step
            for tally in tallies:
                tally.ticks += step
                tally.steps[round(step * 1000 / self.clock_rate)] += 1
        self._count_timing(packet, tallies)
        if key:
            self._mark_key(tallies)

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
        transit = packet.arrived - self._elapsed_ticks / self.clock_rate
        if self._previous_transit is not None:
            change = abs(transit - self._previous_transit)
            self.jitter_s += (change - self.jitter_s) / JITTER_GAIN
            self.max_jitter_s = max(self.max_jitter_s, self.jitter_s)
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

        Берётся наименьшая задержка за минуту: она не зависит от всплесков, и рост значит, что
        очередь в камере или в сети копится.
        """
        delays = [s.min_delay_s for s in self.slices if s.min_delay_s is not None]
        return round(delays[-1] - delays[0], 3) if len(delays) > 1 else 0.0

    def stats(self, step_ms: int | None = None) -> dict:
        """Итог по всему потоку; `step_ms` — заявленный промежуток между кадрами, мс."""
        return {
            **self.total.stats(self.clock_rate, step_ms),
            "gop": self.gop,
            "jitter_ms": round(self.max_jitter_s * 1000, 1),
            "delay_growth_s": self.delay_growth_s,
        }

    def minutes(self, step_ms: int | None = None) -> list[dict]:
        """Итоги по минутам — для таблицы стресс-теста."""
        return [s.stats(self.clock_rate, step_ms) for s in self.slices]
