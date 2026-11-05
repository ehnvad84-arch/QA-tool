"""STREAM-STRESS-1: все потоки камеры читаются разом весь срок стресс-теста.

В стресс-фазе планировщик держит все камеры одновременно, поэтому нагрузка идёт на всю сеть.
Настройки камер не меняются. Потоки читают нити, а не процессы: рабочий процесс камеры — демон,
дочерних процессов ему нельзя. Клиент RTSP не потокобезопасен: у каждой нити свой.
"""

import csv
import io
import json
import threading
import time

from qa_tool.checks.stream.common import (
    MAX_PROFILES,
    RTSP_UNAUTHORIZED,
    add_onvif_evidence,
    client_for,
    limit,
    read_profiles,
    refused_password,
    remember_refusal,
    start_video,
)
from qa_tool.checks.stream.meter import StreamMeter
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Severity,
    Status,
)
from qa_tool.drivers.camera.onvif_generic import MediaProfile
from qa_tool.drivers.camera.rtsp import RtspError, WatchEnd
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault

CHECK_ID = "STREAM-STRESS-1"
SILENCE_S = 10.0  # сценарий S11: молчание дольше считается обрывом
COME_BACK_S = 180.0  # сценарий S11: дольше поток не ждём, остальные камеры продолжают
RETRY_S = 5.0  # реже нельзя заметить возврат, чаще — лишние соединения мешают остальным потокам
RECONNECT_TIMEOUT_S = 5.0  # «Стоп» обещан за секунды, а подключение он не прерывает
LOG_LINES = 200  # за сутки обрывов журнал вырос бы без предела
FIRST_OPEN_S = 15.0  # дольше ждать незачем: следующий поток откроется без него
FINISH_S = 15.0  # после срока: закрыть сессии и собрать итоги
EXPECTED = "поток идёт весь срок без обрывов, потерь и роста задержки"
SECONDS_IN_HOUR = 3600
SLICE_COLUMNS = ("seconds", "packets", "lost", "frames", "dropped", "fps", "kbps")


class StreamWatch(threading.Thread):
    """Нить одного потока: держит его до срока и переподключается после обрыва.

    Поток вернулся, только когда пришёл пакет: камера может отвечать на DESCRIBE и не отдавать
    видео (предел сессий, умерший кодировщик). Время без потока считается от последнего пакета.

    Args:
        ctx: контекст проверки.
        profile: медиапрофиль.
        number: номер потока с 1.
        deadline: конец срока по `time.monotonic()`.
    """

    def __init__(self, ctx: CheckContext, profile: MediaProfile, number: int, deadline: float):
        super().__init__(daemon=True, name=f"stream-{number}")
        self.ctx = ctx
        self.profile = profile
        self.number = number
        self.deadline = deadline
        self.silence_s = float(ctx.extra.get("stream_silence_s", SILENCE_S))
        self.come_back_s = float(ctx.extra.get("stream_come_back_s", COME_BACK_S))
        self.retry_s = float(ctx.extra.get("stream_retry_s", RETRY_S))
        self.slice_s = float(ctx.extra.get("stream_slice_s", 60))
        self.opened = threading.Event()  # решилась судьба первого открытия
        self.playing = False
        self.meter: StreamMeter | None = None
        self.refused = ""
        self.stand_problem = ""
        self.breaks = 0
        self.down_s = 0.0
        self.gone = False
        self.stopped = False
        self.exchanges: list[str] = []
        self._last_packet: float | None = None
        self._lost_at: float | None = None

    def run(self) -> None:
        """Держать поток; сбой нити становится строкой «ошибка стенда»."""
        try:
            self._hold()
        except Exception as e:  # упавшая нить иначе молча оставила бы поток без строки
            self.stand_problem = f"сбой чтения потока: {type(e).__name__}"
        finally:
            self._close_outage()
            self.stopped = self.stopped or (self.ctx.stop_requested() and self._left() > 0)
            self.opened.set()

    def _left(self) -> float:
        return self.deadline - time.monotonic()

    def _heard(self) -> None:
        now = time.monotonic()
        self._last_packet = now
        self._close_outage()

    def _close_outage(self) -> None:
        if self._lost_at is not None:
            self.down_s += time.monotonic() - self._lost_at
            self._lost_at = None

    def _session(self) -> WatchEnd:
        """Одна сессия потока до срока, обрыва или «Стоп»."""
        timeout = RECONNECT_TIMEOUT_S if self.playing else 10.0
        client = client_for(self.ctx, self.profile, timeout)
        try:
            with client:
                _, track = start_video(client)
                if self.meter is None:
                    self.meter = StreamMeter(track.encoding, track.clock_rate, self.slice_s)
                else:
                    self.meter.reconnected()
                self.playing = True
                self.opened.set()
                channel = client.rtp_channel

                def on_packet(packet) -> None:
                    if packet.channel == channel:
                        self._heard()
                        self.meter.add(packet)

                return client.watch(
                    on_packet, self._left(), self.ctx.stop_requested, self.silence_s
                )
        finally:
            self.exchanges = (self.exchanges + client.exchanges)[-LOG_LINES:]

    def _hold(self) -> None:
        while self._left() > 0 and not self.ctx.stop_requested():
            if refused_password(self.ctx):
                self.stand_problem = refused_password(self.ctx)
                return
            try:
                end = self._session()
            except (OSError, RtspError, ValueError) as e:
                if isinstance(e, RtspError) and e.status == RTSP_UNAUTHORIZED:
                    self.stand_problem = remember_refusal(self.ctx, e)
                    return
                if not self.playing:
                    self.refused = str(e)
                    return
                end = WatchEnd.BROKEN
            if end is WatchEnd.STOPPED:
                self.stopped = True
                return
            if end is WatchEnd.TIME_UP:
                return
            if not self._lost():
                return
            self._pause()

    def _lost(self) -> bool:
        """Учесть обрыв или неудачное возвращение; False — ждать больше нечего."""
        now = time.monotonic()
        if self._lost_at is None:
            self._lost_at = self._last_packet or now
            self.breaks += 1
        self.gone = now - self._lost_at >= self.come_back_s
        return not self.gone

    def _pause(self) -> None:
        wake = time.monotonic() + self.retry_s
        while time.monotonic() < wake and not self.ctx.stop_requested():
            time.sleep(min(0.2, self.retry_s))


def delay_problem(ctx: CheckContext, stats: dict) -> str:
    """Что не так с задержкой; пусто — в допуске.

    Рост должен быть и заметным, и быстрым: на сутках только порог в секундах ловил бы уход
    часов камеры, а на коротком прогоне только скорость ловила бы случайные десятые секунды.
    """
    growth, rate = limit(ctx, "stream.delay_growth_s"), limit(ctx, "stream.delay_rate_s_h")
    per_hour = stats["delay_growth_s"] * SECONDS_IN_HOUR / stats["seconds"]
    if stats["delay_growth_s"] > growth.value and per_hour > rate.value:
        return f"задержка выросла на {stats['delay_growth_s']:g} с ({per_hour:.1f} с/ч)"
    return ""


def minutes_csv(meter: StreamMeter) -> str:
    """Таблица по минутам для доказательств."""
    out = io.StringIO()
    table = csv.writer(out)
    table.writerow(["минута", *SLICE_COLUMNS])
    for index, row in enumerate(meter.minutes(), start=1):
        table.writerow([index, *(row[key] for key in SLICE_COLUMNS)])
    return out.getvalue()


def _row(profile: MediaProfile, number: int, result: CheckResult) -> ChildResult:
    return ChildResult(
        f"STREAM-STRESS-{number}",
        f"Поток «{profile.name or profile.token}»: стресс-тест",
        result,
        f"Поток профиля {profile.token} читается весь срок вместе с остальными.",
        Severity.MAJOR,
    )


def verdict(ctx: CheckContext, watch: StreamWatch, held: int) -> CheckResult:
    """Заключение по одному потоку.

    Args:
        ctx: контекст проверки, из него пороги.
        watch: завершившаяся нить потока.
        held: сколько потоков уже шло, когда этот открывался.

    Returns:
        Итог строки; без единого кадра «прошла» не бывает (never.md).
    """
    if watch.stand_problem:
        return CheckResult.stand_error(watch.stand_problem)
    log = Evidence(f"rtsp-stress-{watch.number}.txt", "\n".join(watch.exchanges))
    if watch.refused:
        return CheckResult(
            Status.FAILED,
            reason=f"камера не отдала поток вместе с другими ({held} уже шли): {watch.refused}",
            expected=EXPECTED,
            evidence=[log],
        )
    stats = watch.meter.stats()
    if not stats["seconds"] and watch.stopped and not watch.breaks:
        return CheckResult.skipped("прогон остановлен до первых кадров")
    problems = []
    if not stats["seconds"]:
        problems.append(f"видео не шло: пришло кадров {stats['frames']}")
    if watch.gone:
        problems.append(f"поток не вернулся за {watch.come_back_s:g} с")
    if watch.breaks:
        problems.append(f"обрывов {watch.breaks}, без потока {watch.down_s:.0f} с")
    if stats["lost"]:
        problems.append(f"камера потеряла {stats['lost']} пакетов ({stats['loss_pct']:g} %)")
    if stats["drop_pct"] > limit(ctx, "stream.drop_pct").value:
        problems.append(f"пропущено {stats['dropped']} кадров ({stats['drop_pct']:g} %)")
    delay = delay_problem(ctx, stats) if stats["seconds"] else ""
    if delay:
        problems.append(delay)
    note = "; прервано оператором" if watch.stopped else ""
    summary = {**stats, "breaks": watch.breaks, "down_s": round(watch.down_s, 1)}
    return CheckResult(
        Status.FAILED if problems else Status.PASSED,
        reason="; ".join(problems),
        expected=EXPECTED,
        actual=f"{stats['seconds']:g} с, {stats['fps']:g} кадр/с, {stats['kbps']:g} кбит/с, "
        f"потеряно {stats['lost']}, пропущено кадров {stats['dropped']}, "
        f"обрывов {watch.breaks}, без потока {watch.down_s:.0f} с, "
        f"рост задержки {stats['delay_growth_s']:g} с{note}",
        evidence=[
            Evidence(
                f"stress-{watch.number}.json", json.dumps(summary, ensure_ascii=False, indent=1)
            ),
            Evidence(f"stress-{watch.number}.csv", minutes_csv(watch.meter)),
            log,
        ],
    )


@REGISTRY.check(
    CHECK_ID,
    module="stream",
    title="Стресс-тест потоков",
    description="Все медиапрофили всех камер читаются одновременно весь заданный срок. Считаются "
    "обрывы, потери, пропуски кадров и рост задержки. Настройки камеры не меняются.",
    phase=Phase.STRESS,
    stress=True,
    order=100,
    # к сроку добавляется длительность из сценария; проверка кончается сама, это запас на
    # чтение профилей, открытие потоков и закрытие. Три минуты S11 в запас не входят: ожидание
    # возврата потока ограничено концом срока стресс-теста
    timeout_s=MAX_PROFILES * FIRST_OPEN_S + FINISH_S + 60,
    group="Поток",
    help_id="stream",
)
def stream_stress(ctx: CheckContext) -> CheckResult:
    """Открыть все потоки по очереди и держать их до конца срока."""
    try:
        profiles, evidence = read_profiles(ctx)
    except (CameraUnreachable, OnvifFault) as e:
        return CheckResult(Status.FAILED, reason=f"ONVIF: {e}", expected="профили читаются")
    seconds = float(ctx.extra.get("stress_seconds", ctx.stress_minutes * 60))
    deadline = time.monotonic() + seconds
    watches: list[StreamWatch] = []
    rows: list[ChildResult] = []
    held: dict[int, int] = {}
    for number, profile in enumerate(profiles, start=1):
        if number > MAX_PROFILES:
            reason = f"профилей больше {MAX_PROFILES}: на этот не хватает срока проверки"
            rows.append(_row(profile, number, CheckResult.skipped(reason)))
        elif refused_password(ctx):
            rows.append(_row(profile, number, CheckResult.skipped(refused_password(ctx))))
        elif not profile.uri.startswith("rtsp://"):
            reason = "нет адреса потока: см. строку параметров"
            rows.append(_row(profile, number, CheckResult.skipped(reason)))
        else:
            watch = StreamWatch(ctx, profile, number, deadline)
            held[number] = sum(w.playing for w in watches)
            watch.start()
            # по одному: так видно, какой по счёту поток камера уже не выдержала
            watch.opened.wait(FIRST_OPEN_S)
            watches.append(watch)
    for watch in watches:
        watch.join(max(deadline - time.monotonic(), 0) + FINISH_S)
    for watch in watches:
        if watch.is_alive():
            result = CheckResult.stand_error("поток не закрылся в срок")
        elif watch.meter is None and not watch.refused and not watch.stand_problem:
            result = CheckResult.skipped("прогон остановлен")
        else:
            result = verdict(ctx, watch, held[watch.number])
        rows.append(_row(watch.profile, watch.number, result))
    rows.sort(key=lambda row: int(row.check_id.rsplit("-", 1)[1]))
    add_onvif_evidence(rows, evidence)
    return CheckResult(
        Status.PASSED, actual=f"потоков: {len(profiles)}", evidence=evidence, children=rows
    )
