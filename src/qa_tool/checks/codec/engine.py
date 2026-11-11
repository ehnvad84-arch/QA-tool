"""Движок `CODEC-QUALITY-1`: кривая «качество — битрейт» по каждому кодировщику и кодеку
(спека codec-quality, S1–S8).

Порядок на кодировщик (`A` — текущий кодек камеры, `B` — второй объявленный): `A` на максимуме
(эталон-начало) → ступени `A` → смена на `B` на максимуме, ступени `B` → смена на `A` на
максимуме (повтор эталона: сцена-конец, S7) → возврат и сверка → расчёт качества. Считается
после возврата: ffmpeg нагружает процессор, а камера к этому времени уже как была.

Каждая запись — после журнала (`core/write_journal.py`, вид `CODEC`) через общий проход
`checks/write_pass.py`; возврат кодировщика — в `finally` сразу после его ступеней, в конце —
перечитать все кодировщики и сверить со снимком (AD-3, never.md). Без галочки «изменяющие»
ничего не пишется: строки кодеков «пропущена», текущие настройки — в `CODEC-STATE-1` (S5).
"""

import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.checks.codec import quality
from qa_tool.checks.codec.ladder import Range, ladder
from qa_tool.checks.codec.quality import Clip, FrameMismatch, Quality
from qa_tool.checks.codec.rules import (
    Curve,
    Point,
    choose_reference,
    codec_key,
    codec_title,
    limit,
    num,
    rate,
    rules_problem,
    scene_problem,
    verdict,
    vmaf_class,
)
from qa_tool.checks.image import rules as image_rules
from qa_tool.checks.image.frames import (
    Capture,
    FrameTooLarge,
    RtspFrameSource,
    StreamRefused,
    StreamUnavailable,
    Timings,
)
from qa_tool.checks.image.scene import moved
from qa_tool.checks.onvif.replay import soap_evidence
from qa_tool.checks.rtsp_refusal import refused_password
from qa_tool.checks.stream.common import CODEC_FORMATS, DecoderFailed, ToolMissing
from qa_tool.checks.stream.meter import StreamMeter
from qa_tool.checks.write_pass import (
    LOST,
    STOPPED,
    Abort,
    WritePass,
    lost_abort,
    restore_row,
    total,
)
from qa_tool.core.checks import CheckContext, CheckResult, ChildResult, Evidence, Severity, Status
from qa_tool.core.models import Threshold
from qa_tool.core.secrets import redact
from qa_tool.core.taboo import TabooViolation
from qa_tool.core.tempfiles import CODEC_PREFIX
from qa_tool.core.write_journal import CODEC, PendingWrite
from qa_tool.drivers.camera.encoder import (
    CEILING,
    CODECS,
    CONSTANT,
    BadRange,
    CodecOptions,
    EncoderConfig,
    EncoderWriter,
    EncoderWriteRefused,
    RestoreReport,
    Unsupported,
    ValueRejected,
    mismatch,
)
from qa_tool.drivers.camera.encoder import compare as compare_config
from qa_tool.drivers.camera.onvif_generic import EncoderSettings, service_urls
from qa_tool.drivers.camera.soap import (
    CameraUnreachable,
    OnvifFault,
    SoapClient,
    credentials_rejected,
)

CHECK_ID = "CODEC-QUALITY-1"
STATE_ID = "CODEC-STATE-1"
RESTORE_ID = "CODEC-RESTORE-1"
TIMEOUT_S = 3600.0  # спека, «Время и ресурсы»: 3 кодировщика ≈ 12 мин; запас на медленный стенд
BUDGET_MARGIN_S = 300.0  # столько оставляем от срока на возврат и сверку
MAX_ENCODERS = 4  # спека: больше — фаза выходит за 20 мин; лишние кодировщики «пропущена»
MIN_FREE_BYTES = 500 << 20  # худший кодировщик — ≈ 350 МБ выборок (спека, «Время и ресурсы»)
CLIP_LIMIT = 8 << 20  # выборка в доказательствах — только у «не прошла» и не больше
CAMERA_CLIPS_LIMIT = 64 << 20
NOT_ALLOWED = "не разрешено сценарием: без галочки «изменяющие» кодировщики не меняются"
NOTHING_CHANGED = "ничего не менялось: галочка «изменяющие» не стоит"
NOTHING_WRITTEN = "ничего не менялось: до записи не дошло"
OUT_OF_TIME = "не уложились в срок проверки"
EXTENSIONS = {"H264": "h264", "H265": "h265"}


@dataclass
class Shot:
    """Одна запись и выборка: что задали, что камера дала, где выборка на диске.

    `refused` — камера не приняла значение (S4), `no_stream` — поток не вернулся после записи,
    `error` — стенд не снял выборку («ошибка стенда»). Замер есть, только если все три пусты.
    """

    codec: str
    kbps: int
    form: str = ""
    read_kbps: float | None = None
    capture: Capture | None = None
    clip: Clip | None = None
    measured_kbps: float | None = None
    refused: str = ""
    no_stream: str = ""
    error: str = ""

    @property
    def problem(self) -> str:
        """Почему замера нет; пусто — выборка снята."""
        return self.refused or self.no_stream or self.error


@dataclass
class Plan:
    """Что проверяем у кодировщика: кодеки, их диапазоны, ступени и строки без замера."""

    config: EncoderConfig
    number: int
    a: str
    b: str | None
    options: dict
    steps: dict[str, list[int]] = field(default_factory=dict)
    preset: dict[str, CheckResult] = field(default_factory=dict)  # строки, решённые до записи
    note: str = ""


@dataclass
class Taken:
    """Выборки кодировщика: эталоны в начале и в конце и ступени по кодекам."""

    top: dict[str, Shot] = field(default_factory=dict)
    steps: dict[str, list[Shot]] = field(default_factory=dict)
    repeat: Shot | None = None


def row_id(codec: str, number: int) -> str:
    """ID строки кодека: CODEC-H264-1, CODEC-H265-2."""
    return f"CODEC-{codec}-{number}"


def profile_names(config: EncoderConfig) -> str:
    """Профили кодировщика для протокола: имена, а если имени нет — токены."""
    return ", ".join(name or token for token, name in config.profiles) or config.token


def label(settings: EncoderSettings) -> str:
    """Настройки кодировщика одной строкой журнала: «H.264 4096 кбит/с пост.»."""
    kbps = "?" if settings.kbps is None else f"{settings.kbps:g}"
    mode = {True: "пост.", False: "перем.", None: "режим не сообщён"}[settings.constant]
    return f"{codec_title(settings.encoding)} {kbps} кбит/с {mode}"


def _jpeg(name: str, capture: Capture | None) -> list[Evidence]:
    if capture is None:
        return []
    return [
        Evidence(f"{name}.jpg", capture.preview_jpeg, kind="image"),
        Evidence(f"{name}_t.jpg", capture.thumb_jpeg, kind="image"),
    ]


def _stat(found: Quality | None) -> dict | None:
    if found is None:
        return None
    return {
        "vmaf": None if found.vmaf is None else [found.vmaf.mean, found.vmaf.sd, found.vmaf.low],
        "ssim": [found.ssim.mean, found.ssim.sd, found.ssim.low],
        "psnr": [found.psnr.mean, found.psnr.sd, found.psnr.low],
        "кадров": found.frames,
    }


class Engine:
    """Проход проверки кодеков по одной камере.

    Не потокобезопасен: один движок — один рабочий процесс камеры.

    Args:
        ctx: контекст проверки; `ctx.allow_writes` — галочка «изменяющие».
    """

    def __init__(self, ctx: CheckContext):
        self.ctx = ctx
        self.soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
        self.writer: EncoderWriter | None = None
        self.configs: list[EncoderConfig] = []
        self.rows: list[ChildResult] = []
        self.touched: list[str] = []  # токены кодировщиков, которые писались
        self.writes = WritePass(ctx, CODEC)
        self.deadline = time.monotonic() + TIMEOUT_S - BUDGET_MARGIN_S
        self.folder: Path | None = None
        self.rtsp: list[str] = []
        self._clips_left = CAMERA_CLIPS_LIMIT
        self._current = ""  # ID строки, на которой идёт замер: ей достаётся итог обрыва
        self._total = 0  # кодировщиков в проходе — для строки хода «Профиль 1 из 3»
        self._calc: list = []  # кодировщик, сделано и всего сравнений — для строки хода
        self._restore_notes: list[str] = []

    # --- проход --------------------------------------------------------------------------------

    def run(self) -> CheckResult:
        """Пройти кодировщики; вернуть составной результат со строками."""
        try:
            return self._run()
        finally:
            if self.folder is not None:
                shutil.rmtree(self.folder, ignore_errors=True)

    def _run(self) -> CheckResult:
        refused = refused_password(self.ctx)
        if refused:
            return CheckResult.skipped(refused)
        try:
            services = service_urls(self.soap, self.ctx.camera)
            self.writer = EncoderWriter(self.soap, services)
            self.configs = self.writer.configs()
            self.writes.settle_old(
                self._now_of, self._put_back, (EncoderWriteRefused, ValueRejected)
            )
            self.rows.extend(self.writes.rows)
            # возврат журнала прошлого прогона мог поменять кодировщик — снимок теперь новый
            self.configs = [self.writer.snapshots[c.token] for c in self.configs]
            plans = [self._plan(c, n) for n, c in enumerate(self.configs, start=1)]
        except (CameraUnreachable, OnvifFault) as e:
            return CheckResult.stand_error(
                f"ONVIF: кодировщики не прочитаны: {e}", evidence=soap_evidence(self.soap)
            )
        checked, extra = plans[:MAX_ENCODERS], plans[MAX_ENCODERS:]
        self._total = len(checked)
        self.rows.append(self._state_row(plans, self._current_streams(checked)))
        for plan in extra:
            self._fill(plan, CheckResult.skipped(f"проверены первые {MAX_ENCODERS} кодировщика"))
        if not self.ctx.allow_writes:
            for plan in checked:
                self._fill(plan, CheckResult.skipped(NOT_ALLOWED))
            self.rows.append(self._restore_row(None, ""))
            return total(self.rows)
        broken = rules_problem(self.ctx)
        if broken:
            for plan in checked:
                self._fill(plan, CheckResult.stand_error(f"проверьте thresholds.yaml: {broken}"))
            self.rows.append(self._restore_row(None, ""))
            return total(self.rows)
        try:
            self._encoders(checked)
        finally:
            report, problem = self._restore_all()
            self.rows.append(self._restore_row(report, problem))
        return total(self.rows)

    def _current_streams(self, plans: list[Plan]) -> list[Shot]:
        """Кадр и битрейт текущего потока каждого кодировщика — без записи (S5)."""
        shots = []
        for plan in plans:
            settings = plan.config.settings
            shot = Shot(settings.encoding, int(settings.kbps or 0))
            self._say(plan, "текущий поток")
            try:
                self._capture(plan, shot, plan.config.uri)
            except (
                StreamRefused,
                StreamUnavailable,
                FrameTooLarge,
                ToolMissing,
                DecoderFailed,
            ) as e:
                shot.error = str(e)
            shots.append(shot)
        self.rtsp = []  # обмен текущего потока — не доказательство ступеней
        return shots

    def _encoders(self, plans: list[Plan]) -> None:
        """Кодировщики по очереди; обрыв на одном — остальные «ошибка стенда»."""
        for index, plan in enumerate(plans):
            if self.ctx.stop_requested():
                for rest in plans[index:]:
                    self._fill(rest, CheckResult.skipped(STOPPED))
                return
            try:
                self._encoder(plan)
            except (Abort, CameraUnreachable) as e:
                stop = e if isinstance(e, Abort) else lost_abort(e)
                self.writes.lost = self.writes.lost or stop.current.reason == LOST
                if self._current:
                    self._put(self._current, plan, stop.current)
                self._fill(plan, CheckResult.stand_error(stop.rest))
                for rest in plans[index + 1 :]:
                    self._fill(rest, CheckResult.stand_error(stop.rest))
                return
            except Exception as e:  # сбой на одном кодировщике не должен унести строки и возврат
                text = redact(f"сбой проверки: {type(e).__name__}: {e}", [self.ctx.password])
                self._fill(plan, CheckResult.stand_error(text))
            finally:
                self._current = ""

    # --- план кодировщика ----------------------------------------------------------------------

    def _plan(self, config: EncoderConfig, number: int) -> Plan:
        """Кодеки и ступени кодировщика; строки, которые решаются без записи, — в `preset`.

        Raises:
            CameraUnreachable, OnvifFault: варианты не прочитаны.
        """
        assert self.writer is not None
        options = self.writer.options(config.token)
        a = config.settings.encoding
        plan = Plan(config, number, a if a in CODECS else "", None, options)
        if not plan.a:
            reason = f"текущий кодек — {a}: кривую строим только для H.264 и H.265"
            for codec in CODECS:
                plan.preset[codec] = CheckResult.skipped(reason)
            return plan
        found = options.get(a)
        if not isinstance(found, CodecOptions):
            plan.preset[a] = self._unusable(found)
            for codec in CODECS:
                plan.preset.setdefault(
                    codec, CheckResult.skipped("текущий кодек камеры не проверить")
                )
            return plan
        other = next(c for c in CODECS if c != a)
        second = options.get(other)
        if not isinstance(second, CodecOptions):
            plan.preset[other] = self._unusable(second)
        elif config.service != "media2":
            plan.preset[other] = CheckResult.skipped(
                "камера даёт кодировщики только по Media1, а в ней смены кодека нет"
            )
        elif mismatch(config.settings, second):
            plan.preset[other] = CheckResult.skipped(
                f"{codec_title(other)}: {mismatch(config.settings, second)}"
            )
        else:
            plan.b = other
        height = config.settings.height or 0
        span_a = Range(int(found.kbps_min), int(found.kbps_max))
        span_b = None
        if plan.b:
            assert isinstance(second, CodecOptions)
            span_b = Range(int(second.kbps_min), int(second.kbps_max))
        steps = ladder(self.ctx, height, span_a, span_b)
        plan.note = steps.note
        plan.steps[a] = steps.steps_a
        if plan.b:
            plan.steps[plan.b] = steps.steps_b
        for codec in (a, plan.b):
            if codec and not plan.steps.get(codec):
                plan.preset[codec] = CheckResult.skipped(
                    steps.note or "диапазон камеры не покрывает сетку битрейтов"
                )
        if a in plan.preset:
            # без своих ступеней эталон всё равно снимаем, если есть второй кодек: кривой нет
            # только у этого кодека, а второму эталон нужен
            plan.steps[a] = []
        return plan

    @staticmethod
    def _unusable(found) -> CheckResult:
        if isinstance(found, BadRange):
            return CheckResult(Status.FAILED, reason=f"неверный диапазон: {found.reason}")
        if isinstance(found, Unsupported):
            return CheckResult.skipped(found.reason)
        return CheckResult.skipped("камера не объявляет этот кодек")

    # --- замер кодировщика ---------------------------------------------------------------------

    def _encoder(self, plan: Plan) -> None:
        """Снять, вернуть, посчитать и записать строки одного кодировщика.

        Raises:
            Abort, CameraUnreachable: камера пропала, отвергла пароль или вышел срок.
        """
        for codec, result in plan.preset.items():
            self._put(row_id(codec, plan.number), plan, result)
        if not plan.a or (plan.a in plan.preset and not plan.b):
            return
        if shutil.disk_usage(self._folder()).free < MIN_FREE_BYTES:
            self._fill(plan, CheckResult.stand_error("мало места во временной папке: нужно 500 МБ"))
            return
        exchanges_from = len(self.soap.exchanges)
        taken = Taken()
        try:
            self._measure(plan, taken)
        finally:
            if plan.config.token in self.touched:
                self._restore_encoder(plan)
        self._results(plan, taken, exchanges_from)

    def _measure(self, plan: Plan, taken: Taken) -> None:
        """Эталон `A`, ступени `A`, смена на `B` и его ступени, повтор эталона `A`."""
        a, b = plan.a, plan.b
        top_a = int(plan.options[a].kbps_max)
        self._current = row_id(a, plan.number)
        taken.top[a] = self._shot(plan, a, top_a, f"эталон в начале · {_named(a, top_a)}")
        if taken.top[a].problem:
            return
        taken.steps[a] = self._steps(plan, a)
        if b:
            self._current = row_id(b, plan.number)
            top_b = int(plan.options[b].kbps_max)
            what = f"эталон второго кодека · {_named(b, top_b)}"
            taken.top[b] = self._shot(plan, b, top_b, what, switching=True)
            if not taken.top[b].problem:
                taken.steps[b] = self._steps(plan, b)
        self._current = row_id(a, plan.number)
        what = f"эталон в конце · {_named(a, top_a)}"
        taken.repeat = self._shot(plan, a, top_a, what, switching=bool(b))

    def _steps(self, plan: Plan, codec: str) -> list[Shot]:
        steps = plan.steps.get(codec, [])
        return [
            self._shot(plan, codec, kbps, f"{_named(codec, kbps)} · ступень {i} из {len(steps)}")
            for i, kbps in enumerate(steps, start=1)
        ]

    def _shot(self, plan: Plan, codec: str, kbps: int, what: str, switching: bool = False) -> Shot:
        """Записать кодек и битрейт, дождаться потока и снять выборку.

        Raises:
            Abort: вышел срок или камера отвергла пароль видео или ONVIF.
            CameraUnreachable: камера не отвечает по ONVIF.
        """
        assert self.writer is not None
        self._check_time()
        config = plan.config
        self._say(plan, what)
        shot = Shot(codec, kbps)
        target = EncoderSettings(codec, kbps=kbps, constant=True)
        self._journal(config, target, "intent")
        if config.token not in self.touched:
            self.touched.append(config.token)
        try:
            report = self.writer.write(config.token, codec, kbps, constant=True)
        except ValueRejected as e:
            shot.refused = f"камера не приняла {codec_title(codec)} {rate(kbps)}: {e}"
            return shot
        except OnvifFault as e:
            if credentials_rejected(e):
                reason = f"камера не приняла логин или пароль ONVIF: {e}"
                raise Abort(CheckResult.stand_error(reason), reason) from e
            raise
        except (EncoderWriteRefused, TabooViolation) as e:
            shot.error = f"запись не отправлена: {e}"
            return shot
        finally:
            self._journal(config, target, "sent")
        shot.form = report.form
        back = report.read_back
        shot.read_kbps = None if back is None else back.settings.kbps
        if not report.accepted:
            got = "ничего" if back is None else label(back.settings)
            shot.refused = (
                f"камера не приняла значение: записано {codec_title(codec)} {rate(kbps)}, "
                f"читается {got}"
            )
            return shot
        time.sleep(limit(self.ctx, "codec.apply_delay_s").value)
        self._sample(plan, shot, switching)
        return shot

    def _sample(self, plan: Plan, shot: Shot, switching: bool) -> None:
        """Выборка после записи; после смены кодека адрес потока спрашивается заново.

        Поток не открылся или кодек в нём не тот — пауза `restart_wait_s` и один повтор
        (камера перезапускает кодировщик); снова нет, а ONVIF отвечает — поток не вернулся.
        """
        assert self.writer is not None
        token = plan.config.token
        uri = self.writer.stream_uri(token) if switching else plan.config.uri
        for attempt in range(2):
            if attempt:
                time.sleep(limit(self.ctx, "codec.restart_wait_s").value)
                self._check_time()
                uri = self.writer.stream_uri(token)  # CameraUnreachable — обрыв, а не поток
            try:
                self._capture(plan, shot, uri)
                return
            except StreamRefused as e:
                raise Abort(CheckResult.stand_error(str(e)), str(e)) from e
            except StreamUnavailable as e:
                shot.no_stream = f"поток не вернулся после смены настройки: {e}"
            except (FrameTooLarge, ToolMissing, DecoderFailed) as e:
                shot.error = f"выборка не снята: {e}"
                return

    def _capture(self, plan: Plan, shot: Shot, uri: str) -> None:
        """Одна выборка с ключевого кадра; битрейт — по пакетам той же выборки."""
        if not uri.startswith("rtsp://"):
            raise StreamUnavailable(f"камера не сообщила адрес RTSP: {uri!r}")
        settings = plan.config.settings
        clip_s = limit(self.ctx, "codec.clip_s").value
        if settings.gop and settings.fps:
            clip_s = max(clip_s, 2 * settings.gop / settings.fps)
        timings = Timings(
            keyframe_wait_s=limit(self.ctx, "codec.keyframe_wait_s").value,
            clip_s=min(clip_s, limit(self.ctx, "codec.clip_max_s").value),
            settle_frames=2,
            settle_max_s=0.0,
        )
        source = RtspFrameSource(self.ctx, uri, timings, prefix=CODEC_PREFIX)
        meter = StreamMeter(shot.codec)
        try:
            capture = source.capture(keep_clip=True, on_packet=meter.add)
        finally:
            self.rtsp.extend(source.exchanges)
        got = capture.codec.upper().replace("HEVC", "H265")
        if got != shot.codec:
            raise StreamUnavailable(f"в потоке {got}, а записан {shot.codec}")
        if got not in CODEC_FORMATS or capture.clip is None:
            raise DecoderFailed(f"кодек {got} стенд не сравнивает")
        index = len(list(self._folder().glob("*.*")))
        path = self._folder() / f"{index:02d}.{EXTENSIONS.get(got, 'bin')}"
        path.write_bytes(capture.clip)
        capture.clip = None  # выборка на диске; в памяти держать десятки МБ незачем
        shot.capture = capture
        shot.clip = Clip(path, got, capture.width, capture.height)
        shot.measured_kbps = meter.stats()["kbps"]
        shot.no_stream = ""

    # --- расчёт и строки -----------------------------------------------------------------------

    def _results(self, plan: Plan, taken: Taken, exchanges_from: int) -> None:
        """Эталон, качество каждой ступени к нему, итог и строки кодировщика."""
        a, b = plan.a, plan.b
        self._calc = [plan, 0, self._compares(taken)]
        spread = limit(self.ctx, "codec.vmaf_spread").value
        top_a, top_b = taken.top.get(a), taken.top.get(b) if b else None
        cross: dict[str, Quality | None] = {}
        errors: dict[str, list[str]] = {a: [], **({b: []} if b else {})}
        if b and top_a and top_b and top_a.clip and top_b.clip:
            cross[b], fail_b = self._quality(top_b, top_a)
            cross[a], fail_a = self._quality(top_a, top_b)
            errors[b] += fail_b
            errors[a] += fail_a
        reference, why = choose_reference(cross.get(b or ""), cross.get(a), a, b or a, a, spread)
        ref = taken.top.get(reference)
        repeat_q, repeat_errors = (None, [])
        drift = ""
        if top_a and top_a.clip and taken.repeat and taken.repeat.clip:
            repeat_q, repeat_errors = self._quality(taken.repeat, top_a)
            if top_a.capture and taken.repeat.capture:
                drift = moved(top_a.capture, taken.repeat.capture, self.ctx)
        scene = scene_problem(self.ctx, repeat_q, drift)
        for codec in (a, b):
            if not codec or row_id(codec, plan.number) in self._done():
                continue
            curve = Curve(
                codec,
                plan.config.settings.width or 0,
                plan.config.settings.height or 0,
                constant=_constant(taken, codec),
                scene=scene,
            )
            top = taken.top.get(codec)
            if top is None or top.problem:
                curve.switch_refused = top.refused or top.no_stream if top else ""
                if top and top.error:
                    errors[codec].append(top.error)
            elif codec != reference and codec in cross:
                # ступень на том же битрейте уже есть — вторая точка сломала бы проверку роста
                if all(s.kbps != top.kbps for s in taken.steps.get(codec, [])):
                    curve.points.append(Point(top.kbps, top.measured_kbps, cross[codec]))
            for shot in taken.steps.get(codec, []):
                found, failed = (None, [])
                if not shot.problem and ref and ref.clip:
                    found, failed = self._quality(shot, ref)
                errors[codec] += failed + ([shot.error] if shot.error else [])
                curve.points.append(
                    Point(shot.kbps, shot.measured_kbps, found, shot.refused, shot.no_stream)
                )
            back = taken.repeat
            if codec == a and back is not None and (back.refused or back.no_stream):
                # возврат на свой кодек после второго — тоже смена настройки: поток обязан
                # подняться (решение директора 08.11), поэтому это ступень «не прошла»
                curve.points.append(
                    Point(back.kbps, refused=back.refused, no_stream=back.no_stream)
                )
            if ref is None or not ref.clip:
                errors[codec].append("эталон не снят")
            record = self._record(plan, codec, taken, curve, reference, why, repeat_q)
            self._row(plan, codec, curve, taken, errors[codec] + repeat_errors, record)
        self._attach_exchanges(plan, exchanges_from)

    def _quality(self, dist: Shot, ref: Shot) -> tuple[Quality | None, list[str]]:
        """Качество выборки `dist` к эталону `ref`; ошибка — текстом, не исключением.

        Raises:
            Abort: вышел срок проверки.
        """
        assert dist.clip is not None and ref.clip is not None
        self._check_time()
        if self._calc:
            self._calc[1] += 1
            self._say(self._calc[0], f"расчёт качества · {self._calc[1]} из {self._calc[2]}")
        work = Path(tempfile.mkdtemp(prefix="vmaf-", dir=self._folder()))
        try:
            return (
                quality.compare(
                    dist.clip,
                    ref.clip,
                    work,
                    int(limit(self.ctx, "codec.frames").value),
                    int(limit(self.ctx, "codec.min_frames").value),
                ),
                [],
            )
        except (ToolMissing, DecoderFailed, FrameMismatch) as e:
            return None, [f"качество не посчитано: {e}"]
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _row(
        self,
        plan: Plan,
        codec: str,
        curve: Curve,
        taken: Taken,
        errors: list[str],
        record: dict,
    ) -> None:
        found = verdict(self.ctx, curve)
        reason = found.reason
        if found.status is Status.STAND_ERROR and errors:
            reason = f"{reason}: {'; '.join(dict.fromkeys(errors))}"
        floor = limit(self.ctx, _floor_key(codec, curve.height)).value
        pct = limit(self.ctx, "codec.bitrate_pct").value
        allowed = f"в пределах ±{pct:g} % заданного"
        if not curve.constant:
            # спека: без постоянного битрейта ступень — потолок переменного, пометка в строке
            reason = f"{reason}; {CEILING}: постоянного камера не даёт"
            allowed = f"не выше заданного более чем на {pct:g} %"
        expected = (
            f"VMAF не ниже {num(found.threshold.value)} на ступенях от {rate(floor)}; битрейт "
            f"в потоке — {allowed}; качество растёт с битрейтом"
        )
        record["итог"] = {"статус": found.status.value, "причина": reason}
        evidence = self._evidence(plan, codec, taken, found.status is Status.FAILED, record)
        result = CheckResult(
            found.status,
            reason=reason,
            expected=expected,
            actual=_points_text(curve),
            threshold=found.threshold,
            evidence=evidence,
        )
        self._put(row_id(codec, plan.number), plan, result, found.severity)

    def _record(
        self,
        plan: Plan,
        codec: str,
        taken: Taken,
        curve: Curve,
        reference: str,
        why: str,
        repeat_q: Quality | None,
    ) -> dict:
        """`measure.json`: всё, из чего сложился итог строки, — для протокола и сравнения."""
        settings = plan.config.settings
        options = plan.options.get(codec)
        top = taken.top.get(reference)
        lumas = [
            None if s is None or s.capture is None or not s.capture.lumas else s.capture.lumas[0]
            for s in (taken.top.get(plan.a), taken.repeat)
        ]
        return {
            "версия": 1,
            "кодировщик": {
                "токен": plan.config.token,
                "служба": plan.config.service,
                "профили": [list(p) for p in plan.config.profiles],
            },
            "кодек": codec,
            "профиль кодека": plan.config.codec_profile,
            "разрешение": [settings.width, settings.height],
            "частота": settings.fps,
            "GOP": settings.gop,
            "диапазон": (
                [options.kbps_min, options.kbps_max] if isinstance(options, CodecOptions) else None
            ),
            "эталон": {
                "кодек": reference,
                "битрейт": None if top is None else top.kbps,
                "почему": why,
            },
            "предел замера": _stat(repeat_q),
            "режим битрейта": CONSTANT if curve.constant else CEILING,
            "яркость сцены": lumas,
            "ступени": [
                {
                    "задано": p.kbps,
                    "в потоке": p.measured_kbps,
                    "не принято": p.refused,
                    "поток не вернулся": p.no_stream,
                    "качество": _stat(p.quality),
                }
                for p in sorted(curve.points, key=lambda p: p.kbps)
            ],
            "правило": {
                key: _threshold(limit(self.ctx, key))
                for key in (
                    _vmaf_key(codec, curve.height),
                    _floor_key(codec, curve.height),
                    "codec.bitrate_pct",
                    "codec.vmaf_spread",
                    "codec.repeat_vmaf_min",
                )
            },
            "модель VMAF": quality.VMAF_MODEL,
            "пометка": plan.note,
        }

    def _evidence(
        self, plan: Plan, codec: str, taken: Taken, failed: bool, record: dict
    ) -> list[Evidence]:
        top = taken.top.get(codec)
        evidence = _jpeg("ref", top.capture if top else None)
        shots = taken.steps.get(codec, [])
        for i, shot in enumerate(shots, start=1):
            evidence += _jpeg(f"s{i}", shot.capture)
        evidence.append(Evidence("measure.json", json.dumps(record, ensure_ascii=False, indent=1)))
        if failed:
            for name, shot in [("ref", top), *((f"s{i}", s) for i, s in enumerate(shots, 1))]:
                evidence += self._clip(name, shot)
        return evidence

    def _clip(self, name: str, shot: Shot | None) -> list[Evidence]:
        """Выборка в доказательства «не прошла» — в пределах `CLIP_LIMIT` и запаса камеры."""
        if shot is None or shot.clip is None or not shot.clip.path.exists():
            return []
        size = shot.clip.path.stat().st_size
        if size > CLIP_LIMIT or size > self._clips_left:
            text = f"выборка {size >> 20} МБ — сверх предела, не сохранена"
            return [Evidence(f"{name}.txt", text)]
        self._clips_left -= size
        extension = EXTENSIONS.get(shot.clip.codec, "bin")
        return [Evidence(f"{name}.{extension}", shot.clip.path.read_bytes(), kind="video")]

    def _attach_exchanges(self, plan: Plan, exchanges_from: int) -> None:
        """Обмен ONVIF и RTSP кодировщика — к его строкам с замером."""
        exchanges = [
            Evidence(
                f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}"
            )
            for i, x in enumerate(self.soap.exchanges[exchanges_from:], start=1)
        ]
        if self.rtsp:
            exchanges.append(Evidence("rtsp.txt", "\n".join(self.rtsp)))
        self.rtsp = []
        ids = {row_id(c, plan.number) for c in CODECS}
        for row in self.rows:
            if row.check_id in ids and row.result.status is not Status.SKIPPED:
                row.result.evidence = [*row.result.evidence, *exchanges]

    # --- возврат -------------------------------------------------------------------------------

    def _restore_encoder(self, plan: Plan) -> None:
        """Вернуть кодировщик сразу после его ступеней; не вернулся — скажет строка возврата.

        Raises:
            CameraUnreachable: камера пропала — дальше решает обрыв в `_encoders`.
        """
        assert self.writer is not None
        config = plan.config
        snapshot = self.writer.snapshots[config.token]
        self._journal(config, snapshot.settings, "restoring")
        self._say(plan, "возврат настроек")
        try:
            report = self.writer.restore(snapshot)
        except (OnvifFault, ValueRejected, EncoderWriteRefused) as e:
            self._restore_notes.append(f"{profile_names(config)}: возврат не выполнен: {e}")
            return
        if report.restored:
            self.writes.close()

    def _restore_all(self) -> tuple[RestoreReport | None, str]:
        """В конце — перечитать все кодировщики и вернуть, что не как в снимке.

        Камера могла сама перенастроить соседний поток из-за предела кодировщиков, поэтому
        сверяются все, а не только записанные. Ошибки не выбрасываются — итог в строке возврата.
        """
        if self.writer is None or not self.touched:
            return None, ""
        wait = image_rules.limit(self.ctx, "image.reconnect_wait_s").value
        absent = self.writes.camera_back(wait)
        if absent:
            return None, absent
        total_report = RestoreReport(other=list(self._restore_notes))
        try:
            for config in self.configs:
                snapshot = self.writer.snapshots[config.token]
                report = compare_config(snapshot, self.writer.read(config.token))
                if report.left:
                    self._journal(config, snapshot.settings, "restoring")
                    report = self.writer.restore(snapshot)
                names = profile_names(config)
                total_report.left += [(f"{names}: {f}", was, now) for f, was, now in report.left]
                total_report.other += [f"{names}: {text}" for text in report.other]
        except (CameraUnreachable, OnvifFault, ValueRejected, EncoderWriteRefused) as e:
            return None, f"возврат не выполнен: {e}"
        if total_report.restored:
            self.writes.close()
        return total_report, ""

    # --- журнал --------------------------------------------------------------------------------

    def _journal(self, config: EncoderConfig, target: EncoderSettings, stage: str) -> None:
        assert self.writer is not None
        snapshot = self.writer.snapshots[config.token]
        self.writes.note(
            self.writer.services[config.service],
            f"кодировщик {profile_names(config)}",
            config.token,
            label(snapshot.settings),
            label(target),
            stage,
            {"token": config.token, "service": config.service, "xml": snapshot.xml},
        )

    def _now_of(self, entry: PendingWrite) -> str:
        """Текущие настройки кодировщика из журнала прошлого прогона — в виде журнала."""
        assert self.writer is not None
        if entry.kind not in self.writer.snapshots:
            return "кодировщика с этим токеном у камеры нет"
        return label(self.writer.read(entry.kind).settings)

    def _put_back(self, entry: PendingWrite) -> str:
        """Вернуть конфигурацию из журнала прошлого прогона и прочитать заново."""
        assert self.writer is not None
        service = entry.restore.get("service", "")
        if service not in ("media2", "media"):
            raise EncoderWriteRefused("в журнале нет службы медиа")
        self.writer.restore_xml(service, entry.kind, entry.restore["xml"])
        self.writer.snapshots.pop(entry.kind, None)  # снимок — теперь возвращённые настройки
        return label(self.writer.read(entry.kind).settings)

    # --- строки --------------------------------------------------------------------------------

    def _done(self) -> set[str]:
        return {row.check_id for row in self.rows}

    def _put(
        self, check_id: str, plan: Plan, result: CheckResult, severity: Severity | None = None
    ) -> None:
        """Строка кодека, если её ещё нет: первый итог строки — окончательный."""
        if check_id in self._done():
            return
        codec = check_id.split("-")[1]
        title = f"Кодеки: {codec_title(codec)} — {profile_names(plan.config)}"
        description = (
            f"Ставим {codec_title(codec)} на максимум камеры (эталон) и на ступени общей сетки "
            "битрейтов при постоянном битрейте, снимаем выборку потока на каждой ступени и "
            "считаем VMAF, SSIM и PSNR к эталону; ожидаем, что качество не ниже порога, растёт "
            "с битрейтом, а поток держит заданный битрейт."
        )
        self.rows.append(ChildResult(check_id, title, result, description, severity))

    def _fill(self, plan: Plan, result: CheckResult) -> None:
        """Все ещё не записанные строки кодировщика — одним итогом."""
        for codec in CODECS:
            copy = CheckResult(result.status, reason=result.reason, expected=result.expected)
            self._put(row_id(codec, plan.number), plan, copy)

    def _state_row(self, plans: list[Plan], current: list[Shot]) -> ChildResult:
        lines = []
        evidence: list[Evidence] = []
        for plan, shot in zip(plans, current, strict=False):
            evidence += _jpeg(f"state-{plan.number}", shot.capture)
        for index, plan in enumerate(plans):
            s = plan.config.settings
            spans = []
            for codec in CODECS:
                found = plan.options.get(codec)
                if isinstance(found, CodecOptions):
                    spans.append(
                        f"{codec_title(codec)} {found.kbps_min:g}…{found.kbps_max:g} кбит/с"
                    )
                else:
                    spans.append(f"{codec_title(codec)}: {getattr(found, 'reason', 'нет')}")
            stream = "не проверялся"
            if index < len(current):
                shot = current[index]
                stream = (
                    f"кадр не снят: {shot.error}"
                    if shot.measured_kbps is None
                    else f"в потоке {num(shot.measured_kbps)} кбит/с"
                )
            lines.append(
                f"{profile_names(plan.config)}: {label(s)}, {s.width}×{s.height}, "
                f"{s.fps or '?'} к/с, GOP {s.gop or '?'}; {'; '.join(spans)}; {stream}"
            )
        result = CheckResult(
            Status.PASSED,
            reason=f"кодировщиков: {len(plans)}, настройки прочитаны",
            expected="текущие настройки кодировщиков и их диапазоны прочитаны",
            actual="; ".join(lines[:2]),
            evidence=[*evidence, Evidence("state.txt", "\n".join(lines))]
            + [Evidence(f"encoder-{p.number}.xml", p.config.xml) for p in plans],
        )
        description = "Читаем текущие настройки кодировщиков видео и диапазоны кодеков."
        return ChildResult(
            STATE_ID, "Кодеки: текущие настройки", result, description, Severity.MINOR
        )

    def _restore_row(self, report: RestoreReport | None, problem: str) -> ChildResult:
        names = [profile_names(c) for c in self.configs if c.token in self.touched]
        return restore_row(
            RESTORE_ID,
            "Кодеки: возврат настроек",
            names,
            report,
            problem,
            NOTHING_WRITTEN if self.ctx.allow_writes else NOTHING_CHANGED,
        )

    # --- служебное -----------------------------------------------------------------------------

    def _folder(self) -> Path:
        if self.folder is None:
            self.folder = Path(tempfile.mkdtemp(prefix=f"{CODEC_PREFIX}{self.ctx.camera.slot}-"))
        return self.folder

    def _say(self, plan: Plan, text: str) -> None:
        """Строка хода для окна: «Профиль 1 из 3 · H.265 · 2 Мбит/с · ступень 3 из 5»."""
        self.ctx.progress(f"Профиль {plan.number} из {max(self._total, plan.number)} · {text}")

    @staticmethod
    def _compares(taken: Taken) -> int:
        """Сколько сравнений качества впереди: перекрёстные эталоны, повтор и ступени."""
        shots = [s for steps in taken.steps.values() for s in steps if s.clip]
        cross = 2 if len([t for t in taken.top.values() if t.clip]) == 2 else 0
        repeat = 1 if taken.repeat and taken.repeat.clip else 0
        return len(shots) + cross + repeat

    def _check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise Abort(CheckResult.stand_error(OUT_OF_TIME), OUT_OF_TIME)


def _named(codec: str, kbps: int) -> str:
    return f"{codec_title(codec)} · {rate(kbps)}"


def _constant(taken: Taken, codec: str) -> bool:
    """Битрейт кодека ушёл постоянным; драйвер ставит потолок переменного у Media1 и у камер
    без `ConstantBitRateSupported`, и тогда поток ниже заданного — норма, а не дефект."""
    shots = [taken.top.get(codec), *taken.steps.get(codec, [])]
    return all(s.form != CEILING for s in shots if s is not None)


def _threshold(found: Threshold) -> dict:
    return {"значение": found.value, "источник": found.source}


def _vmaf_key(codec: str, height: int) -> str:
    return f"codec.{codec_key(codec)}.{vmaf_class(height)}.vmaf_min"


def _floor_key(codec: str, height: int) -> str:
    return f"codec.{codec_key(codec)}.{vmaf_class(height)}.kbps_floor"


def _points_text(curve: Curve) -> str:
    """«0,5 / 1 / 2 Мбит/с → VMAF 71,3 / 80,2 / 88,0» — графа «Факт», от меньшего битрейта."""
    points = sorted(curve.points, key=lambda p: p.kbps)
    if not points:
        return "ступеней нет"
    rates = " / ".join(f"{p.kbps / 1024:.3g}".replace(".", ",") for p in points)
    if all(p.quality is None or p.quality.vmaf is None for p in points):
        name, values = "SSIM", [p.quality.ssim.mean if p.quality else None for p in points]
        shown = [f"{v:.3f}".replace(".", ",") if v is not None else "—" for v in values]
    else:
        name = "VMAF"
        shown = [num(p.quality.vmaf.mean) if p.quality and p.quality.vmaf else "—" for p in points]
    return f"{rates} Мбит/с → {name} {' / '.join(shown)}"
