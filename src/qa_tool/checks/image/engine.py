"""Движок `IMAGE-EFFECT-1`: каждая настройка картинки — в 0, 50 и 100 % диапазона камеры, кадр
в каждом положении, величина, итог и возврат (спека этапа 7, S1–S9).

Порядок на настройку: 0 % → 50 % → 100 % (запись, чтение назад, пауза, стабильный кадр,
величина), затем возврат исходного блока и контрольный кадр, затем `verdict()`. Перед каждой
записью — журнал (`core/write_journal.py`, вид `IMAGE`): если процесс камеры снимут посреди
записи, планировщик и следующий прогон узнают, что вернуть; журнал, обрыв и строку возврата
ведёт общий проход `checks/write_pass.py`. Возврат — в `finally`: камера не уходит со стенда
изменённой ни при каком исходе (AD-3, never.md).

Без галочки «изменяющие» настройки не меняются: строки настроек «пропущена», а кадр и текущие
значения всё равно идут в протокол (S7, строка `IMAGE-STATE-1`).
"""

import json
import time
from dataclasses import dataclass, field

from qa_tool.checks.image.frames import (
    Capture,
    FrameTooLarge,
    RtspFrameSource,
    StreamRefused,
    StreamUnavailable,
)
from qa_tool.checks.image.metrics import Masks, Measurement, masks_of, measure
from qa_tool.checks.image.rules import limit, rule_for, verdict
from qa_tool.checks.image.scene import OrdinaryScene, Scene, moved
from qa_tool.checks.image.settings import METRIC_TITLES, SETTINGS, Exposure, Setting
from qa_tool.checks.onvif.replay import soap_evidence
from qa_tool.checks.onvif.stream import first_stream, video_source
from qa_tool.checks.rtsp_refusal import refused_password
from qa_tool.checks.stream.common import DecoderFailed, ToolMissing
from qa_tool.checks.write_pass import (
    LOST,
    STOPPED,
    Abort,
    WritePass,
    lost_abort,
    restore_row,
    total,
)
from qa_tool.core.checks import (
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Severity,
    Status,
)
from qa_tool.core.secrets import redact
from qa_tool.core.taboo import TabooViolation
from qa_tool.core.write_journal import IMAGE, PendingWrite
from qa_tool.drivers.camera.imaging import (
    BadRange,
    Declared,
    ImageWriteRefused,
    ImagingState,
    ImagingWriter,
    RestoreReport,
    Unsupported,
    ValueRejected,
)
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import (
    CameraUnreachable,
    OnvifFault,
    SoapClient,
    credentials_rejected,
)

CHECK_ID = "IMAGE-EFFECT-1"
STATE_ID = "IMAGE-STATE-1"
RESTORE_ID = "IMAGE-RESTORE-1"
NOT_ALLOWED = "не разрешено сценарием: без галочки «изменяющие» настройки не меняются"
NOTHING_CHANGED = "ничего не менялось: галочка «изменяющие» не стоит"
SHARES = (0.0, 0.5, 1.0)
SETTINGS_KEY = "image_settings"  # ключи настроек для прогона; нет — все 9 (тесты сокращают)
FRAMES_ERRORS = (StreamRefused, StreamUnavailable, FrameTooLarge, ToolMissing, DecoderFailed)


@dataclass
class Position:
    """Одно положение настройки: что записано, что прочитано, что снято."""

    share: float
    value: float
    read_back: float | None = None
    form: str = ""
    body: str = ""
    capture: Capture | None = None
    measurement: Measurement | None = None

    @property
    def name(self) -> str:
        """Имя положения для файлов: p000, p050, p100."""
        return f"p{round(self.share * 100):03d}"


@dataclass
class Outcome:
    """Строки протокола, накопленные за проверку."""

    rows: list[ChildResult] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)  # ключи настроек, которые писались


def _jpeg(name: str, capture: Capture | None) -> list[Evidence]:
    if capture is None:
        return []
    return [
        Evidence(f"{name}.jpg", capture.preview_jpeg, kind="image"),
        Evidence(f"{name}_t.jpg", capture.thumb_jpeg, kind="image"),
    ]


def _positions_text(positions: list["Position"], setting: Setting) -> str:
    """«0 % — 82,4; 50 % — 121,0; 100 % — 163,5 (средняя яркость кадра)» — графа «Факт»."""
    two = len(positions) == 2
    parts = []
    for index, p in enumerate(positions):
        if p.measurement is None:
            continue
        label = ("выкл", "вкл")[index] if two else f"{round(p.share * 100)} %"
        parts.append(f"{label} — {p.measurement.value:.1f}".replace(".", ","))
    return f"{'; '.join(parts)} ({METRIC_TITLES[setting.metric]}, {setting.unit})"


def _shown(value: float | None) -> str:
    return "—" if value is None else f"{value:.6g}"


class Engine:
    """Проход проверки картинки по одной камере.

    Args:
        ctx: контекст проверки; `ctx.allow_writes` — галочка «изменяющие».
        scene: сцена; по умолчанию — обычная (этап 7).
    """

    def __init__(self, ctx: CheckContext, scene: Scene | None = None):
        self.ctx = ctx
        self.scene = scene or OrdinaryScene()
        chosen = ctx.extra.get(SETTINGS_KEY)
        self.settings: list[Setting] = [s for s in SETTINGS if not chosen or s.key in chosen]
        self.soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
        self.writer: ImagingWriter | None = None
        self.frames: RtspFrameSource | None = None
        self.state: ImagingState | None = None
        self.out = Outcome()
        self._last_written = 0.0  # для журнала на стадии возврата: что могло остаться
        self.ranges: dict = {}
        self.writes = WritePass(ctx, IMAGE)

    # --- проход --------------------------------------------------------------------------------

    def run(self) -> CheckResult:
        """Пройти все настройки; вернуть составной результат со строками."""
        refused = refused_password(self.ctx)
        if refused:
            return CheckResult.skipped(refused)
        try:
            services = service_urls(self.soap, self.ctx.camera)
            if "imaging" not in services:
                return self._all_skipped("камера не объявляет службу Imaging (ONVIF)")
            _profile, uri, source = first_stream(self.soap, services)
            source = video_source(self.soap, services, source)
            # читаем все настройки, даже если проверяем часть: журнал прошлого прогона может
            # касаться любой из них
            fields = {s.key: s for s in SETTINGS}
            self.writer = ImagingWriter(self.soap, services["imaging"], source, fields)
            ranges = self.writer.declared()
            self.ranges = ranges
            self.state = self.writer.read()
        except (CameraUnreachable, OnvifFault) as e:
            return CheckResult.stand_error(
                f"ONVIF: настройки картинки не прочитаны: {e}", evidence=soap_evidence(self.soap)
            )
        if not uri.startswith("rtsp://"):
            return CheckResult.stand_error(f"камера не сообщила адрес RTSP: {uri!r}")
        try:
            self.writes.settle_old(self._now_of, self._put_back, (ImageWriteRefused,))
            self.out.rows.extend(self.writes.rows)
        except (CameraUnreachable, OnvifFault) as e:
            return CheckResult.stand_error(
                f"ONVIF: журнал прошлого прогона не разобран: {e}",
                evidence=soap_evidence(self.soap),
            )
        self.frames = RtspFrameSource(self.ctx, uri)
        try:
            unready = self.scene.prepare(self.ctx)
            if unready:
                return CheckResult.stand_error(unready)
            self._progress("кадры «до»")
            before = self.frames.capture()
            time.sleep(limit(self.ctx, "image.before_gap_s").value)
            second = self.frames.capture()
        except FRAMES_ERRORS as e:
            return self._frames_failed(e)
        self.out.rows.append(self._state_row(ranges, before))
        unstable = moved(before, second, self.ctx)
        if not self.ctx.allow_writes:
            self._read_only_rows(ranges)
            return self._result()
        if unstable:
            for setting in self.settings:
                self._row(setting, CheckResult.stand_error(unstable), before)
            self.out.rows.append(self._restore_row(None, NOTHING_CHANGED))
            return self._result()
        masks = masks_of(before.work)
        number = 0
        try:
            for number, setting in enumerate(self.settings, start=1):
                if self.ctx.stop_requested():
                    for rest in self.settings[number - 1 :]:
                        self._row(rest, CheckResult.skipped(STOPPED), before)
                    break
                try:
                    self._one(setting, ranges[setting.key], before, masks, number)
                except (Abort, CameraUnreachable) as e:
                    # камера могла пропасть и во время возврата после сбоя — это тоже обрыв
                    stop = e if isinstance(e, Abort) else lost_abort(e)
                    self.writes.lost = self.writes.lost or stop.current.reason == LOST
                    self._row(setting, stop.current, before)
                    for rest in self.settings[number:]:
                        self._row(rest, CheckResult.stand_error(stop.rest), before)
                    break
                except Exception as e:  # сбой на одной настройке не должен унести строки и возврат
                    text = redact(f"сбой проверки: {type(e).__name__}: {e}", [self.ctx.password])
                    self._row(setting, CheckResult.stand_error(text), before)
        finally:
            report, problem = self._restore_all()
            self.out.rows.append(self._restore_row(report, problem))
        return self._result()

    def _one(self, setting: Setting, declared, before: Capture, masks: Masks, number: int) -> None:
        """Одна настройка: положения, возврат, контрольный кадр, итог.

        Raises:
            Abort: камера пропала, отвергла пароль или поток — дальше проверять нечем.
        """
        assert self.writer is not None and self.state is not None
        skip = self._skip_reason(setting, declared, before)
        if skip is not None:
            self._row(setting, skip, before)
            return
        rule = rule_for(self.ctx, setting.key)
        assert isinstance(declared, Declared)
        shares = (0.0, 1.0) if declared.two_positions else SHARES
        positions = [Position(share, declared.at(share)) for share in shares]
        exchanges_from = len(self.soap.exchanges)
        self.out.touched.append(setting.key)
        problem: CheckResult | None = None
        after: Capture | None = None
        try:
            for position in positions:
                self._progress(
                    f"{setting.title} · {round(position.share * 100)} % · "
                    f"{number} из {len(self.settings)}"
                )
                rejected = self._write(setting, position)
                if rejected:
                    problem = CheckResult(Status.FAILED, reason=rejected, expected=rule.expected)
                    break
                self._stop_on_protected_drift(setting, position)
                time.sleep(limit(self.ctx, "image.apply_delay_s").value)
                position.capture = self._capture()
                position.measurement = measure(setting.metric, position.capture, masks)
            back = self._restore_one(setting)
            after = self._capture()
        except (ImageWriteRefused, TabooViolation) as e:
            problem = CheckResult.stand_error(f"запись не отправлена: {e}")
            back = self._restore_after_error(setting)
        except FRAMES_ERRORS as e:
            if isinstance(e, StreamRefused):
                raise Abort(CheckResult.stand_error(str(e)), str(e)) from e
            problem = CheckResult.stand_error(f"кадр не получен: {e}")
            back = self._restore_after_error(setting)
        except OnvifFault as e:
            if credentials_rejected(e):
                reason = f"камера не приняла логин или пароль ONVIF: {e}"
                raise Abort(CheckResult.stand_error(reason), reason) from e
            problem = CheckResult.stand_error(f"ONVIF: {e}")
            back = self._restore_after_error(setting)
        except CameraUnreachable as e:
            raise lost_abort(e, rule.expected) from e
        evidence = self._evidence(setting, before, positions, after, exchanges_from, rule)
        if back:
            # итог по положениям верен, а то, что камера осталась изменённой, скажет строка
            # возврата; здесь — пометка для разбора
            evidence.append(Evidence("restore.txt", back))
        if problem is not None:
            self._row(setting, problem, before, evidence)
            return
        drift = moved(before, after, self.ctx) if after is not None else ""
        if drift:
            self._row(
                setting,
                CheckResult(
                    Status.STAND_ERROR, reason=f"после возврата {drift}", expected=rule.expected
                ),
                before,
                evidence,
            )
            return
        values = [p.measurement.value for p in positions if p.measurement]
        clips = [
            max(p.measurement.clip_pct, p.measurement.crush_pct) for p in positions if p.measurement
        ]
        found = verdict(rule, values, clips, self.state.exposure_mode)
        result = CheckResult(
            found.status,
            reason=found.reason,
            expected=rule.expected,
            actual=_positions_text(positions, setting),
            threshold=rule.step,
        )
        self._row(setting, result, before, evidence, found.severity)

    def _stop_on_protected_drift(self, setting: Setting, position: Position) -> None:
        """Камера сама изменила экспозицию, ИК-фильтр или фокус после записи — дальше не писать.

        Такой побочный эффект может не вернуться (Hikvision .111, 09.11: MaxGain 100 → 0), и
        каждая следующая запись множила бы риск. Текущая настройка — «не прошла», остальные —
        «ошибка стенда»; общий возврат и строка возврата (блокер) — как при обрыве.

        Raises:
            Abort: защищённый блок изменился.
        """
        assert self.writer is not None
        drift = self.writer.protected_drift()
        if not drift:
            return
        changes = "; ".join(f"{name}: было {was}, стало {now}" for name, was, now in drift)
        reason = (
            f"камера сама изменила то, что программа не пишет, после записи «{setting.title}» "
            f"{round(position.share * 100)} %: {changes}"
        )
        failed = CheckResult(
            Status.FAILED,
            reason=reason,
            expected="запись настройки не меняет экспозицию, ИК-фильтр и фокус",
        )
        raise Abort(failed, "проверка остановлена: камера меняет защищённые настройки")

    def _skip_reason(self, setting: Setting, declared, before: Capture) -> CheckResult | None:
        """Итог без записи: настройки нет, диапазон негоден, сцена или экспозиция не те."""
        assert self.state is not None
        if isinstance(declared, Unsupported):
            return CheckResult.skipped(declared.reason)
        if isinstance(declared, BadRange):
            return CheckResult(Status.FAILED, reason=f"неверный диапазон: {declared.reason}")
        unmet = self.scene.unmet(setting.key, before, self.ctx)
        if unmet:
            return CheckResult.skipped(unmet)
        if setting.exposure is Exposure.NEEDS_AUTO and self.state.exposure_mode == "MANUAL":
            return CheckResult.skipped(
                "компенсация засветки работает только при автоэкспозиции, а она выключена"
            )
        rule = rule_for(self.ctx, setting.key)
        if rule.problem:
            return CheckResult.stand_error(f"проверьте thresholds.yaml: {rule.problem}")
        return None

    def _capture(self) -> Capture:
        assert self.frames is not None
        return self.frames.capture()

    def _write(self, setting: Setting, position: Position) -> str:
        """Записать положение; причина «не прошла», если камера не приняла значение."""
        assert self.writer is not None
        self._last_written = position.value
        self._journal(setting, position.value, "intent")
        try:
            report = self.writer.write(setting.key, position.value)
        except ValueRejected as e:
            return f"камера не приняла значение {_shown(position.value)}: {e}"
        finally:
            self._journal(setting, position.value, "sent")
        position.read_back, position.form, position.body = (
            report.read_back,
            report.form,
            report.body,
        )
        if not report.accepted:
            return (
                f"камера не приняла значение: записано {_shown(position.value)}, "
                f"читается {_shown(report.read_back)}"
            )
        return ""

    def _restore_after_error(self, setting: Setting) -> str:
        """Вернуть настройку сразу после сбоя: иначе следующие настройки снимались бы с ней.

        Raises:
            CameraUnreachable: камера пропала — дальше решает `Abort` в `_one`.
        """
        try:
            return self._restore_one(setting)
        except (OnvifFault, ImageWriteRefused, TabooViolation) as e:
            return f"возврат после сбоя не выполнен: {e}"

    def _restore_one(self, setting: Setting) -> str:
        """Вернуть исходный блок после настройки; текст — что не вернулось."""
        assert self.writer is not None and self.state is not None
        self._journal(setting, self._last_written, "restoring")
        report = self.writer.restore(self.state, [setting.key])
        if report.restored:
            self.writes.close()
        return "; ".join(f"{name}: было {was}, стало {now}" for name, was, now in report.left)

    def _restore_all(self) -> tuple[RestoreReport | None, str]:
        """Полный возврат в конце; ошибки не выбрасываются — их итог в строке возврата."""
        if self.writer is None or self.state is None or not self.out.touched:
            return None, ""
        absent = self.writes.camera_back(limit(self.ctx, "image.reconnect_wait_s").value)
        if absent:
            return None, absent
        try:
            report = self.writer.restore(self.state, self.out.touched)
        except (CameraUnreachable, OnvifFault, ImageWriteRefused) as e:
            return None, f"возврат не выполнен: {e}"
        if report.restored:
            self.writes.close()
        return report, ""

    # --- журнал --------------------------------------------------------------------------------

    def _now_of(self, entry: PendingWrite) -> str:
        """Текущее значение настройки из журнала прошлого прогона — в виде журнала."""
        assert self.state is not None
        return _shown(self.state.values.get(entry.kind))

    def _put_back(self, entry: PendingWrite) -> str:
        """Вернуть блок из журнала прошлого прогона и прочитать значение заново."""
        assert self.writer is not None
        self.writer.restore_xml(entry.restore["xml"])
        self.writer.baseline = None  # снимок — теперь уже возвращённые настройки
        self.state = self.writer.read()
        return self._now_of(entry)

    def _journal(self, setting: Setting, value: float, stage: str) -> None:
        if self.state is None or self.writer is None:
            return
        self.writes.note(
            self.writer.url,
            setting.title,
            setting.key,
            _shown(self.state.values.get(setting.key)),
            _shown(value),
            stage,
            {"token": self.writer.source, "xml": self.state.xml},
        )

    # --- строки --------------------------------------------------------------------------------

    def _row(
        self,
        setting: Setting,
        result: CheckResult,
        before: Capture,
        evidence: list[Evidence] | None = None,
        severity: Severity | None = None,
    ) -> None:
        if result.status is not Status.PASSED and not evidence:
            evidence = _jpeg("before1", before)
        result.evidence = [*result.evidence, *(evidence or [])]
        description = (
            f"Ставим «{setting.title}» в 0, 50 и 100 % диапазона камеры и снимаем кадр в каждом "
            f"положении; ожидаем, что {METRIC_TITLES[setting.metric]} меняется в нужную сторону."
        )
        self.out.rows.append(
            ChildResult(setting.check_id, setting.title, result, description, severity)
        )

    def _evidence(
        self,
        setting: Setting,
        before: Capture,
        positions: list[Position],
        after: Capture | None,
        exchanges_from: int,
        rule,
    ) -> list[Evidence]:
        assert self.state is not None
        evidence = _jpeg("before1", before)
        for position in positions:
            evidence += _jpeg(position.name, position.capture)
        evidence += _jpeg("after", after)
        declared = self.ranges.get(setting.key)
        record = {
            "настройка": setting.key,
            "поле": "/".join(setting.path),
            "диапазон": ([declared.min, declared.max] if isinstance(declared, Declared) else None),
            "величина": setting.metric,
            "режим экспозиции": self.state.exposure_mode,
            "исходное": self.state.values.get(setting.key),
            "кадр": {"ширина": before.width, "высота": before.height, "кодек": before.codec},
            "правило": {
                "сторона": rule.direction,
                "шаг": rule.step.value,
                "разброс": rule.stability.value,
                "источник": rule.step.source,
            },
            "положения": [
                {
                    "доля": p.share,
                    "значение": p.value,
                    "прочитано": p.read_back,
                    "форма записи": p.form,
                    "величина": None if p.measurement is None else p.measurement.value,
                    "пересвет %": None if p.measurement is None else p.measurement.clip_pct,
                    "чернота %": None if p.measurement is None else p.measurement.crush_pct,
                    "кадр стабилен": None if p.capture is None else p.capture.stable,
                }
                for p in positions
            ],
        }
        evidence.append(Evidence("measure.json", json.dumps(record, ensure_ascii=False, indent=1)))
        evidence += [
            Evidence(
                f"onvif-{i}.txt", f"POST {x.url}\n{x.request}\n\nHTTP {x.status}\n{x.response}"
            )
            for i, x in enumerate(self.soap.exchanges[exchanges_from:], start=1)
        ]
        return evidence

    def _state_row(self, ranges: dict, before: Capture) -> ChildResult:
        assert self.state is not None
        lines = [f"режим экспозиции: {self.state.exposure_mode or 'не сообщён'}"]
        for setting in self.settings:
            declared = ranges.get(setting.key)
            if isinstance(declared, Declared):
                span = (
                    "выкл/вкл" if declared.two_positions else f"{declared.min:g}…{declared.max:g}"
                )
            else:
                span = getattr(declared, "reason", "нет")
            value = _shown(self.state.values.get(setting.key))
            lines.append(f"{setting.title}: {value} (диапазон {span})")
        evidence = _jpeg("state", before) + [
            Evidence("imaging.xml", self.state.xml),
            Evidence("state.txt", "\n".join(lines)),
        ]
        result = CheckResult(
            Status.PASSED,
            reason=f"кадр {before.width}×{before.height} {before.codec}, значения прочитаны",
            expected="кадр и текущие настройки картинки сняты",
            actual="; ".join(lines[:4]),
            evidence=evidence,
        )
        description = "Снимаем кадр и читаем текущие настройки картинки и их диапазоны."
        return ChildResult(
            STATE_ID, "Картинка: кадр и настройки", result, description, Severity.MINOR
        )

    def _restore_row(self, report: RestoreReport | None, problem: str) -> ChildResult:
        return restore_row(
            RESTORE_ID,
            "Картинка: возврат настроек",
            self.out.touched,
            report,
            problem,
            NOTHING_CHANGED,
        )

    def _read_only_rows(self, ranges: dict) -> None:
        for setting in self.settings:
            declared = ranges.get(setting.key)
            reason = declared.reason if isinstance(declared, Unsupported) else NOT_ALLOWED
            self.out.rows.append(
                ChildResult(setting.check_id, setting.title, CheckResult.skipped(reason))
            )
        self.out.rows.append(self._restore_row(None, NOTHING_CHANGED))

    def _all_skipped(self, reason: str) -> CheckResult:
        rows = [ChildResult(STATE_ID, "Картинка: кадр и настройки", CheckResult.skipped(reason))]
        rows += [
            ChildResult(s.check_id, s.title, CheckResult.skipped(reason)) for s in self.settings
        ]
        rows.append(
            ChildResult(RESTORE_ID, "Картинка: возврат настроек", CheckResult.skipped(reason))
        )
        return CheckResult(Status.SKIPPED, reason=reason, children=rows)

    def _frames_failed(self, error: Exception) -> CheckResult:
        reason = str(error) if isinstance(error, StreamRefused) else f"кадр не получен: {error}"
        evidence = soap_evidence(self.soap)
        if self.frames is not None:
            evidence.append(Evidence("rtsp.txt", "\n".join(self.frames.exchanges)))
        return CheckResult.stand_error(reason, evidence=evidence)

    def _result(self) -> CheckResult:
        return total(self.out.rows)

    def _progress(self, text: str) -> None:
        self.ctx.progress(text)
