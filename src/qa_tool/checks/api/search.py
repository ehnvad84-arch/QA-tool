"""Автопоиск соответствий «ONVIF ↔ родной параметр» (S4, вариант C директора 09.11) и остановка
по побочному эффекту записи (S5).

Только с галочкой «изменяющие», только по ONVIF и только настройки каталога со «S4 пишет»:
картинка через `ImagingWriter` (защищённые блоки не уходят изменёнными), битрейт и кодек через
`EncoderWriter` (белый список). Родной API здесь только читается снимками.

На настройку: запись `v1` → снимок M1 → какие родные параметры сдвинулись (Δ1) → запись `v2` →
снимок M2 → Δ2. Пара найдена, если в Δ2 ровно один параметр (или одна группа копий по профилям),
он же был в Δ1, и его значения ложатся на шкалу ONVIF (число — прямая по трём точкам). После
каждой настройки — возврат по ONVIF и сверка всей наблюдаемой карты с исходной. Сдвинулось больше,
чем записано в ONVIF, или вне ожидаемого раздела, или изменились другие листы ONVIF — побочный
эффект: поиск на камере останавливается (решение директора 09.11, S5).
"""

import contextlib
import fnmatch
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from xml.etree.ElementTree import Element

from qa_tool.checks.api.catalog import BY_N, CATALOG, ENCODER, IMAGE, Entry
from qa_tool.checks.api.compare import number
from qa_tool.checks.image.settings import BY_KEY
from qa_tool.core.api_map import Change, NativeMap, Pair, Scale, canonical, diff, finite_number
from qa_tool.drivers.camera.encoder import CodecOptions, EncoderConfig, EncoderWriter, compare
from qa_tool.drivers.camera.imaging import Declared, ImagingState, ImagingWriter
from qa_tool.drivers.camera.key_value import without_indexes

MIN_GAP = 0.15  # доли диапазона: ближе значения не различить на шкале камеры


class SearchRefused(RuntimeError):
    """Настройку нельзя писать автопоиском (нет в каталоге со «S4 пишет»): запись не уходит."""


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def leaves_of(element: Element) -> dict[str, str]:
    """Листья блока ONVIF по путям из локальных имён: «WhiteBalance/CrGain» → «50»."""
    found: dict[str, str] = {}
    stack = [(element, "")]
    while stack:
        node, path = stack.pop()
        children = [c for c in node if _local(c.tag)]
        if not children and path:
            found[path] = (node.text or "").strip()
        for child in children:
            name = _local(child.tag)
            stack.append((child, f"{path}/{name}" if path else name))
    return found


class Knob(Protocol):
    """Одна настройка ONVIF для автопоиска: значения, запись, листы, возврат."""

    entry: Entry

    def plan(self, low: float, high: float) -> tuple[Any, Any] | str:
        """Два значения записи или причина, почему настройку не пробовать."""
        ...

    def current(self) -> Any:
        """Исходное значение до записи."""
        ...

    def write(self, value: Any, persist: bool = False) -> tuple[bool, Any]:
        """Записать и прочитать назад: (принято, прочитано)."""
        ...

    def leaves(self) -> dict[str, str]:
        """Все листы своей службы ONVIF сейчас."""
        ...

    def mine(self) -> set[str]:
        """Листы, которые меняет запись этой настройки (с режимом и парным усилением)."""
        ...

    def restore(self) -> list[tuple[str, str, str]]:
        """Вернуть снимок по ONVIF; что не вернулось: (настройка, было, стало)."""
        ...

    def verify(self) -> list[tuple[str, str, str]]:
        """Сверить ONVIF со снимком без записи; что не совпало — как у `restore`."""
        ...

    def drift(self) -> list[tuple[str, str, str]]:
        """Защищённые блоки, которые камера сменила сама (только картинка)."""
        ...

    def journal(self) -> dict[str, str]:
        """Что нужно журналу для возврата по ONVIF после снятого процесса."""
        ...

    def below(self, held: float) -> Any | None:
        """Значение ниже предела, который камера держит на деле, или `None` (только битрейт)."""
        ...


def _shares(p0: float, low_share: float, high_share: float) -> list[float]:
    """Две доли из {low, 0,5, high}: дальше всех от текущей и друг от друга (S4 п. 4)."""
    # при равном расстоянии — меньшая доля первой: порядок записей не зависит от хеша множества
    shares = sorted({low_share, 0.5, high_share}, key=lambda s: (-round(abs(s - p0), 9), s))
    picked: list[float] = []
    for share in shares:
        if abs(share - p0) >= MIN_GAP and all(abs(share - s) >= MIN_GAP for s in picked):
            picked.append(share)
    return picked[:2]


@dataclass
class ImagingKnob:
    """Настройка картинки через `ImagingWriter` (ключ «Картинки» из каталога)."""

    entry: Entry
    writer: ImagingWriter
    state0: ImagingState

    @property
    def declared(self) -> Declared | None:
        """Диапазон из GetOptions, если он годный."""
        found = self.writer.ranges.get(self.entry.setting)
        return found if isinstance(found, Declared) and not found.two_positions else None

    def current(self) -> Any:
        """Значение настройки в снимке."""
        return self.state0.values.get(self.entry.setting)

    def plan(self, low: float, high: float) -> tuple[Any, Any] | str:
        """Значения на долях диапазона ONVIF; у режима без уровня — не ищем."""
        span, v0 = self.declared, self.current()
        if span is None:
            return "ONVIF не объявил годный диапазон"
        if v0 is None or span.max == span.min:
            return "ONVIF не сообщил текущее значение"
        picked = _shares((v0 - span.min) / (span.max - span.min), low, high)
        if len(picked) < 2:
            return "диапазон слишком узок для двух записей"
        return span.at(picked[0]), span.at(picked[1])

    def write(self, value: Any, persist: bool = False) -> tuple[bool, Any]:
        """Запись одного поля (защищённые блоки драйвер не пропустит)."""
        report = self.writer.write(self.entry.setting, float(value), persist=persist)
        return report.accepted, report.read_back

    def leaves(self) -> dict[str, str]:
        """Листы ImagingSettings сейчас."""
        return leaves_of(self.writer.read().element)

    def mine(self) -> set[str]:
        """Поле, режим и парное усиление баланса белого."""
        spec = BY_KEY[self.entry.setting]
        return {"/".join(p) for p in (spec.path, spec.mode, spec.partner) if p}

    def restore(self) -> list[tuple[str, str, str]]:
        """Весь снимок картинки с ForcePersistence."""
        return self.writer.restore(self.state0, [self.entry.setting]).left

    def verify(self) -> list[tuple[str, str, str]]:
        """Сверка картинки со снимком без записи."""
        return self.writer.verify(self.state0, [self.entry.setting]).left

    def drift(self) -> list[tuple[str, str, str]]:
        """Сдвиг экспозиции, ИК-фильтра, фокуса относительно снимка."""
        return self.writer.protected_drift()

    def journal(self) -> dict[str, str]:
        """Снимок картинки для `restore_xml`."""
        return {"xml": self.state0.xml, "service": "imaging"}

    def below(self, held: float) -> Any | None:
        """У картинки предела «держит меньше объявленного» нет."""
        return None


def _encoder_leaves(config: EncoderConfig) -> dict[str, str]:
    """Поля кодировщика, которые видит автопоиск, в виде листов."""
    s = config.settings
    return {
        "Encoding": s.encoding or "",
        "RateControl/BitrateLimit": "" if s.kbps is None else number(s.kbps),
        "Resolution": f"{s.width}x{s.height}",
        "RateControl/FrameRateLimit": "" if s.fps is None else number(s.fps),
        "GovLength": "" if s.gop is None else str(s.gop),
    }


@dataclass
class EncoderKnob:
    """Битрейт или кодек кодировщика профиля 1 через `EncoderWriter`."""

    entry: Entry
    writer: EncoderWriter
    config0: EncoderConfig
    options: dict

    @property
    def codec(self) -> str:
        """Кодек снимка."""
        return (self.config0.settings.encoding or "").upper()

    def current(self) -> Any:
        """Битрейт или кодек снимка."""
        if self.entry.setting == "kbps":
            return self.config0.settings.kbps
        return self.codec

    def plan(self, low: float, high: float) -> tuple[Any, Any] | str:
        """Битрейт: середина и край объявленного; кодек: второй объявленный и назад."""
        own = self.options.get(self.codec)
        if not isinstance(own, CodecOptions):
            return "ONVIF не объявил варианты текущего кодека"
        if self.entry.setting == "kbps":
            if own.kbps_max <= own.kbps_min:
                return "ONVIF не объявил годный диапазон битрейта"
            return round((own.kbps_min + own.kbps_max) / 2), own.kbps_max
        other = next(
            (k for k, v in self.options.items() if k != self.codec and isinstance(v, CodecOptions)),
            None,
        )
        if other is None:
            return "камера объявляет один кодек"
        return other, self.codec

    def write(self, value: Any, persist: bool = False) -> tuple[bool, Any]:
        """Запись битрейта при текущем кодеке или смена кодека при допустимом битрейте."""
        s = self.config0.settings
        constant = bool(s.constant) if s.constant is not None else True
        if self.entry.setting == "kbps":
            report = self.writer.write(self.config0.token, self.codec, float(value), constant)
            back = report.read_back.settings.kbps if report.read_back else None
            return report.accepted, back
        target = self.options[value]
        kbps = min(max(s.kbps or target.kbps_min, target.kbps_min), target.kbps_max)
        report = self.writer.write(self.config0.token, value, kbps, constant)
        back = report.read_back.settings.encoding if report.read_back else None
        return report.accepted, back

    def leaves(self) -> dict[str, str]:
        """Поля кодировщика сейчас."""
        return _encoder_leaves(self.writer.read(self.config0.token))

    def mine(self) -> set[str]:
        """Битрейт; у смены кодека — и битрейт: камера держит свой предел для кодека."""
        if self.entry.setting == "kbps":
            return {"RateControl/BitrateLimit"}
        return {"Encoding", "RateControl/BitrateLimit"}

    def restore(self) -> list[tuple[str, str, str]]:
        """Вся конфигурация снимка."""
        return self.writer.restore(self.config0).left

    def verify(self) -> list[tuple[str, str, str]]:
        """Сверка конфигурации со снимком без записи."""
        return compare(self.config0, self.writer.read(self.config0.token)).left

    def drift(self) -> list[tuple[str, str, str]]:
        """У кодировщика защищённых блоков нет."""
        return []

    def journal(self) -> dict[str, str]:
        """Конфигурация для `restore_xml`."""
        return {
            "xml": self.config0.xml,
            "service": self.config0.service,
            "token": self.config0.token,
        }

    def below(self, held: float) -> Any | None:
        """Середина между объявленным минимумом и пределом, который камера держит на деле."""
        own = self.options.get(self.codec)
        if self.entry.setting != "kbps" or not isinstance(own, CodecOptions):
            return None
        value = round((own.kbps_min + held) / 2)
        return value if own.kbps_min <= value < held else None


@dataclass(frozen=True)
class Limits:
    """Пороги автопоиска `api.*`."""

    apply_delay_s: float = 2.0
    share_low: float = 0.2
    share_high: float = 0.8
    residual_pct: float = 2.0
    clamp_pct: float = 2.0
    budget_s: float = 900.0


@dataclass
class SideEffect:
    """Побочный эффект записи (S5): что сдвинулось сверх записанного."""

    entry: Entry
    native: list[Change]
    onvif: list[tuple[str, str, str]]
    outside: list[str]

    def text(self, label: str) -> str:
        """Причина для строки `API-SEARCH-1`."""
        parts = [f"{label} {c.id} {c.before} → {c.after}" for c in self.native]
        parts += [f"ONVIF {name} {was} → {now}" for name, was, now in self.onvif]
        return f"запись ONVIF «{self.entry.title}» сдвинула и другие параметры: " + ", ".join(parts)


@dataclass
class Outcome:
    """Итог автопоиска на камере."""

    pairs: list[Pair] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    tried: int = 0
    stop: SideEffect | None = None
    left: list[tuple[str, str, str]] = field(default_factory=list)
    native_left: list[Change] = field(default_factory=list)
    held: dict[int, tuple[float, str]] = field(default_factory=dict)
    attempts: list[Any] = field(default_factory=list)  # `restore.Attempt` возврата родным API


def _groups(changes: list[Change]) -> dict[tuple[str, Any], list[Change]]:
    """Копии одного параметра (индексы каналов и профилей, одно новое значение) — одна группа."""
    found: dict[tuple[str, Any], list[Change]] = {}
    for change in changes:
        found.setdefault((without_indexes(change.id), canonical(change.after)), []).append(change)
    return found


def _fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Прямая по точкам методом наименьших квадратов: (a, b, наибольшее отклонение)."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return 0.0, my, math.inf
    a = sum((x - mx) * (y - my) for x, y in points) / sxx
    b = my - a * mx
    return a, b, max(abs(y - (a * x + b)) for x, y in points)


class Search:
    """Автопоиск по одной камере. Не потокобезопасен: один процесс камеры.

    Args:
        knobs: настройки в порядке поиска (картинка, битрейт, кодек последним).
        snapshot: снимок наблюдаемых разделов родного API.
        m0: исходный снимок (M0b), `volatile` — изменчивые параметры.
        expected: класс настройки → шаблоны id разделов, где ждём сдвиг.
        note: журнал записи `(entry, old, new, stage, restore)` — до записи и до возврата.
        source: подпись найденной пары («найдено автоматически, прогон …»).
        restorer: возврат родным API параметров, которые не вернул ONVIF (`NativeRestorer.run`);
            `None` — только вручную.
    """

    def __init__(
        self,
        knobs: list[Knob],
        snapshot: Callable[[], NativeMap],
        m0: NativeMap,
        volatile: frozenset[str],
        expected: Callable[[str], tuple[str, ...]],
        note: Callable[[Entry, str, str, str, dict[str, str]], None],
        limits: Limits,
        source: str,
        label: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        progress: Callable[[str], None] = lambda _t: None,
        restorer: Callable[[list[Change], NativeMap], list[Any]] | None = None,
    ) -> None:
        """Подготовить поиск; записи начинаются в `run`."""
        for knob in knobs:
            if not knob.entry.writes or BY_N.get(knob.entry.n) is not knob.entry:
                raise SearchRefused(f"«{knob.entry.title}» автопоиск не пишет")
        self.knobs, self.snapshot, self.m0, self.volatile = knobs, snapshot, m0, volatile
        self.expected, self.note, self.limits = expected, note, limits
        self.source, self.label = source, label
        self.sleep, self.clock, self.progress = sleep, clock, progress
        self.restorer = restorer
        self.out = Outcome()

    # --- снимки и сдвиги --------------------------------------------------------------------

    def _shot(self) -> NativeMap:
        self.sleep(self.limits.apply_delay_s)
        return self.snapshot()

    def _sections(self, native: NativeMap) -> dict[str, str]:
        return {p.id: s.id for s in native.sections for p in s.params}

    def _outside(self, entry: Entry, changes: list[Change], native: NativeMap) -> list[str]:
        """Сдвинувшиеся параметры вне ожидаемых разделов класса настройки."""
        patterns = self.expected(entry.cls)
        where = self._sections(native)
        return [
            c.id
            for c in changes
            if not any(fnmatch.fnmatchcase(where.get(c.id, ""), p) for p in patterns)
        ]

    def _side_effect(
        self,
        knob: Knob,
        changes: list[Change],
        before: dict[str, str],
        after: dict[str, str],
        native: NativeMap,
    ) -> SideEffect | None:
        """Признаки S5: другие листы ONVIF, групп больше записанных листов, чужой раздел."""
        moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        mine = knob.mine()
        other = [(k, before.get(k, "нет"), after.get(k, "нет")) for k in sorted(moved - mine)]
        other += [(name, was, now) for name, was, now in knob.drift()]
        written = max(1, len(moved & mine))
        outside = self._outside(knob.entry, changes, native)
        if other or len(_groups(changes)) > written or outside:
            return SideEffect(knob.entry, changes, other, outside)
        return None

    # --- одна настройка ---------------------------------------------------------------------

    def _try(self, knob: Knob, m_prev: NativeMap) -> tuple[str, Pair | None, NativeMap]:
        """Запись `v1`, `v2` и решение; возврат делает вызывающий."""
        entry = knob.entry
        values = knob.plan(self.limits.share_low, self.limits.share_high)
        if isinstance(values, str):
            return f"пропущена: {values}", None, m_prev
        v0, (v1, v2) = knob.current(), values
        before = knob.leaves()
        self.note(entry, str(v0), str(v1), "intent", knob.journal())
        accepted, back1 = knob.write(v1)
        self.note(entry, str(v0), str(v1), "sent", knob.journal())
        if not accepted:
            self._held(knob, v1, back1)
            # Dahua .108: середина объявленного уже выше того, что камера держит, — первую
            # точку берём ниже предела, иначе пара битрейта и строка диапазона не появятся
            held = self.out.held.get(entry.n)
            lower = knob.below(held[0]) if held else None
            if lower is not None:
                v1 = lower
                self.note(entry, str(v0), str(v1), "intent", knob.journal())
                accepted, back1 = knob.write(v1)
                self.note(entry, str(v0), str(v1), "sent", knob.journal())
        if not accepted:
            return (
                "не найдена: камера не приняла значение по ONVIF (ответ OK, читается прежнее)",
                None,
                m_prev,
            )
        m1 = self._shot()
        after = knob.leaves()
        delta1 = diff(m_prev, m1, self.volatile)
        if not delta1:
            knob.write(v1, persist=True)
            m1 = self._shot()
            after = knob.leaves()
            delta1 = diff(m_prev, m1, self.volatile)
            if not delta1:
                return "не найдена: родной параметр не сдвинулся", None, m1
        effect = self._side_effect(knob, delta1, before, after, m1)
        if effect is not None:
            self.out.stop = effect
            return "остановлен: побочный эффект записи", None, m1
        accepted, back2 = knob.write(v2)
        self.note(entry, str(v0), str(v2), "sent", knob.journal())
        m2 = self._shot()
        delta2 = diff(m1, m2, self.volatile)
        effect = self._side_effect(knob, delta2, after, knob.leaves(), m2)
        if effect is not None:
            self.out.stop = effect
            return "остановлен: побочный эффект записи", None, m2
        self._mode_pair(knob, before, after, delta1, delta2)
        return self._decide(knob, (v0, v1, v2), (back1, back2), delta1, delta2, m2)

    def _mode_pair(
        self,
        knob: Knob,
        before: dict[str, str],
        after: dict[str, str],
        delta1: list[Change],
        delta2: list[Change],
    ) -> None:
        """Пара режима (n 5, 8, 10): родной параметр сдвинулся в Δ1 вместе с режимом ONVIF и
        стоит в Δ2 — запись уровня включила режим, вторая запись его уже не трогала (S4 п. 9)."""
        mode = next((e for e in CATALOG if knob.entry.n in e.mode_of), None)
        if mode is None:
            return
        leaf = "/".join(mode.path)
        was, now = before.get(leaf), after.get(leaf)
        if not was or not now or was == now:
            return
        still = {c.id for c in delta2}
        groups = _groups([c for c in delta1 if c.id not in still])
        if len(groups) != 1:
            return
        change = next(iter(groups.values()))[0]
        values = {was: change.before, now: change.after}
        scale = Scale("перечисление", values=values)
        self.out.pairs.append(
            Pair(mode.n, mode.onvif, change.id, scale, state="найдена", source=self.source)
        )

    def _held(self, knob: Knob, written: Any, back: Any) -> None:
        """Битрейт прочитан меньше записанного: камера объявляет больше, чем держит (.108 09.11)."""
        value, held = finite_number(str(written)), finite_number(str(back))
        if knob.entry.setting != "kbps" or value is None or held is None:
            return
        if held < value * (1 - self.limits.clamp_pct / 100):
            codec = getattr(knob, "codec", "")
            self.out.held[knob.entry.n] = (held, f"при {codec}" if codec else "")

    def _decide(
        self,
        knob: Knob,
        written: tuple,
        back: tuple,
        delta1: list[Change],
        delta2: list[Change],
        m2: NativeMap,
    ) -> tuple[str, Pair | None, NativeMap]:
        """Пара по Δ1 и Δ2: ровно одна группа в Δ2 и она же в Δ1; шкала — прямая или таблица."""
        entry = knob.entry
        groups2 = _groups(delta2)
        if len(groups2) != 1:
            return f"не найдена: после второй записи сдвинулось {len(groups2)} параметров", None, m2
        target = next(iter(groups2.values()))[0]
        if target.id not in {c.id for c in delta1}:
            return "не найдена: второй сдвиг не совпал с первым", None, m2
        n0 = self.m0.params()[target.id].value
        n1 = next(c.after for c in delta1 if c.id == target.id)
        n2 = target.after
        if not entry.numeric:
            values = {str(written[1]): n1, str(written[2]): n2, str(written[0]): n0}
            if len({canonical(v) for v in values.values()}) != len(values):
                return "кандидат: значения не взаимно однозначны", None, m2
            pair = Pair(
                entry.n,
                entry.onvif,
                target.id,
                Scale("перечисление", values=values),
                state="найдена",
                source=self.source,
            )
            return "найдена", pair, m2
        xs = [finite_number(str(v)) for v in written]
        ys = [finite_number(v) for v in (n0, n1, n2)]
        if None in xs or None in ys:
            return "не найдена: значения не числа", None, m2
        points = list(zip(xs, ys, strict=True))
        if entry.setting == "kbps":
            clamped = back[1] is not None and back[1] < xs[2] * (1 - self.limits.clamp_pct / 100)
            if clamped:
                codec = getattr(knob, "codec", "")
                self.out.held[entry.n] = (float(back[1]), f"при {codec}" if codec else "")
                points = points[:2]
        a, b, residual = _fit(points)
        param = self.m0.params()[target.id]
        allowed = param.allowed or {}
        if "мин" in allowed and "макс" in allowed:
            span = float(allowed["макс"]) - float(allowed["мин"])
        else:
            span = abs(a) * (max(xs) - min(xs))
        step = float(allowed.get("шаг", 0) or 0)
        tol = max(step, self.limits.residual_pct / 100 * span, 0.5 if param.type == "целое" else 0)
        state = "найдена" if a != 0 and residual <= tol else "кандидат"
        pair = Pair(
            entry.n,
            entry.onvif,
            target.id,
            Scale("линейная" if state == "найдена" else "по точкам", a=a, b=b),
            points=tuple(points),
            state=state,
            source=self.source,
        )
        reason = "найдена" if state == "найдена" else "кандидат: значения не ложатся на шкалу ONVIF"
        return reason, pair, m2

    # --- проход -----------------------------------------------------------------------------

    def run(self) -> Outcome:
        """Все настройки по порядку с возвратом после каждой; при побочном эффекте — стоп."""
        started = self.clock()
        for index, knob in enumerate(self.knobs):
            entry = knob.entry
            estimate = 3 * (5 + self.limits.apply_delay_s + 5)
            if self.clock() - started + estimate > self.limits.budget_s:
                for rest in self.knobs[index:]:
                    self.out.trace.append(
                        {"n": rest.entry.n, "итог": "не пробовали: не уложились в срок автопоиска"}
                    )
                break
            self.progress(f"автопоиск: {entry.title}")
            self.out.tried += 1
            try:
                verdict, pair, _last = self._try(knob, self.m0)
            except Exception:
                # камера пропала или отказала посреди записи: вернуть по ONVIF что получится;
                # журнал остаётся открытым — следующий прогон увидит незакрытую запись
                with contextlib.suppress(Exception):
                    knob.restore()
                raise
            self.note(entry, str(knob.current()), str(knob.current()), "restoring", knob.journal())
            left, native_left = self._put_back(knob)
            self.out.trace.append({"n": entry.n, "настройка": entry.title, "итог": verdict})
            if pair is not None:
                self.out.pairs.append(pair)
            if left or native_left:
                self.out.left, self.out.native_left = left, native_left
                if self.out.stop is None:
                    self.out.stop = SideEffect(entry, native_left, left, [])
            if self.out.stop is not None:
                n = self.knobs.index(knob)
                for rest in self.knobs[n + 1 :]:
                    self.out.trace.append(
                        {
                            "n": rest.entry.n,
                            "итог": f"не пробовали: поиск остановлен на п. {entry.n}",
                        }
                    )
                break
        return self.out

    def _put_back(self, knob: Knob) -> tuple[list[tuple[str, str, str]], list[Change]]:
        """Возврат по ONVIF, затем родным API того, что ONVIF не вернул (S5), и сверка с M0."""
        left = knob.restore()
        m3 = self.snapshot()
        native_left = diff(self.m0, m3, self.volatile)
        if not native_left or self.restorer is None:
            return left, native_left
        attempts = self.restorer(native_left, m3)
        self.out.attempts = attempts
        if not any(a.sent for a in attempts):
            return left, native_left
        # у пары вроде GainLevel ↔ MaxGain ONVIF возвращается следом: только перечитать —
        # повторная запись снимка по ONVIF сбила бы то, что вернул родной API
        self.sleep(self.limits.apply_delay_s)
        left = knob.verify()
        return left, diff(self.m0, self.snapshot(), self.volatile)

    def trace_json(self) -> str:
        """`search.json`: трасса по настройкам и найденное."""
        data = {
            "настройки": self.out.trace,
            "пары": [{"n": p.n, "родной": p.native, "состояние": p.state} for p in self.out.pairs],
        }
        return json.dumps(data, ensure_ascii=False, indent=1)


def knobs_for(
    imaging: ImagingWriter | None,
    state0: ImagingState | None,
    encoder: EncoderWriter | None,
    config0: EncoderConfig | None,
    options: dict,
    known: set[int],
) -> list[Knob]:
    """Настройки для поиска: каталог «S4 пишет», кроме уже известных пар; кодек — последним."""
    knobs: list[Knob] = []
    for entry in CATALOG:
        if not entry.writes or entry.n in known:
            continue
        if entry.cls == IMAGE and imaging is not None and state0 is not None:
            knobs.append(ImagingKnob(entry, imaging, state0))
        elif entry.cls == ENCODER and encoder is not None and config0 is not None:
            knobs.append(EncoderKnob(entry, encoder, config0, options))
    return sorted(knobs, key=lambda k: (k.entry.setting == "encoding", k.entry.n))
