"""Автопоиск внутри `API-MAP-1` (S4, S5): ONVIF-писатели, журнал `API`, снимки родного API,
строки `API-SEARCH-1` и `API-RESTORE-1`.

Порядок: журнал прошлого прогона (`settle_old`, только ONVIF) → два исходных снимка наблюдаемых
разделов с паузой (изменчивые параметры) → `Search.run` → строки. Журнал пишется до первой записи
и перед каждым возвратом; закрывается, только если всё вернулось.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from qa_tool.checks.api.catalog import BY_N, Entry
from qa_tool.checks.api.limits import limit
from qa_tool.checks.api.restore import NativeRestorer, Send, old_native_lines
from qa_tool.checks.api.search import Limits, Outcome, Search, knobs_for
from qa_tool.checks.image.settings import SETTINGS
from qa_tool.checks.onvif.stream import first_stream
from qa_tool.checks.write_pass import WritePass
from qa_tool.core.api_map import NativeMap, Pair, Section, volatile_ids
from qa_tool.core.checks import CheckContext, CheckResult, ChildResult, Evidence, Severity, Status
from qa_tool.core.write_journal import API, RUN_ID_KEY, PendingWrite, load
from qa_tool.drivers.camera.encoder import EncoderWriter, EncoderWriteRefused
from qa_tool.drivers.camera.imaging import ImageWriteRefused, ImagingWriter
from qa_tool.drivers.camera.native import Collector, NativeReader, ReadTemplate
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

SEARCH_ID, RESTORE_ID = "API-SEARCH-1", "API-RESTORE-1"


class Observed:
    """Сборщик для снимков: только наблюдаемые разделы, шаблоны — по полной карте M0.

    Шаблоны потоков Hikvision строятся из списка потоков, которого среди наблюдаемых нет, поэтому
    `reads` получает разделы полной карты, а не прочитанные в снимке.
    """

    def __init__(self, inner: Collector, base: list[Section], ids: set[str]) -> None:
        """Обёртка над сборщиком `inner` с разделами `ids` из карты `base`."""
        self.inner, self.base, self.ids = inner, base, ids
        self.brand, self.name, self.version = inner.brand, inner.name, inner.version
        self.names, self.login = inner.names, inner.login
        self.max_requests = getattr(inner, "max_requests", 80)

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Шаблоны наблюдаемых разделов."""
        return [t for t in self.inner.reads(self.base) if t.section_id in self.ids]

    def parse(self, template, body, content_type, describe):
        """Разбор — как у сборщика."""
        return self.inner.parse(template, body, content_type, describe)

    def expected(self, kind):
        """Ожидаемые разделы — как у сборщика."""
        return self.inner.expected(kind)

    def observed(self, native):
        """Наблюдаемые разделы — как у сборщика."""
        return self.inner.observed(native)

    def restore_request(self, param, m0):
        """Возврат — как у сборщика."""
        return self.inner.restore_request(param, m0)


@dataclass
class SearchReport:
    """Что автопоиск добавляет к `API-MAP-1`."""

    rows: list[ChildResult] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    held: dict[int, tuple[float, str]] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)


def _row(check_id: str, title: str, result: CheckResult, severity: Severity) -> ChildResult:
    return ChildResult(check_id, title, result, severity=severity)


def _search_row(outcome: Outcome, label: str, restored: bool) -> ChildResult:
    """`API-SEARCH-1`: найдено пар K из L; при побочном эффекте — «не прошла» (мелкая)."""
    expected = "запись одной настройки ONVIF меняет в родном API ровно один параметр"
    found = sum(p.state == "найдена" for p in outcome.pairs)
    if outcome.stop is not None:
        tail = "после возврата всё вернулось" if restored else "вернулось не всё — см. возврат"
        result = CheckResult(
            Status.FAILED, expected=expected, actual=f"{outcome.stop.text(label)}; {tail}"
        )
    else:
        result = CheckResult(
            Status.PASSED,
            expected=expected,
            actual=f"найдено пар {found} из {outcome.tried} настроек",
        )
    return _row(SEARCH_ID, "Автопоиск соответствий", result, Severity.MINOR)


def _restore_row(outcome: Outcome, label: str) -> ChildResult:
    """`API-RESTORE-1` (блокер): всё ли вернулось после автопоиска."""
    expected = "все настройки ONVIF и вся наблюдаемая карта родного API — как до записи"
    if outcome.tried == 0:
        result = CheckResult.skipped("ничего не менялось")
    elif outcome.left or outcome.native_left:
        lines = [f"ONVIF {name}: было {was}, осталось {now}" for name, was, now in outcome.left]
        how = {a.change.id: a.how() for a in outcome.attempts}
        lines += [
            f"{label} {c.id}: было {c.before}, осталось {c.after} — вернуть вручную: "
            + how.get(c.id, "веб-интерфейс камеры, раздел изображения")
            for c in outcome.native_left
        ]
        result = CheckResult(
            Status.FAILED,
            reason="камера осталась изменённой",
            expected=expected,
            actual="; ".join(lines),
        )
    else:
        result = CheckResult(Status.PASSED, expected=expected, actual="всё вернулось")
    return _row(RESTORE_ID, "Возврат после автопоиска", result, Severity.BLOCKER)


def _restore_facts(outcome: Outcome, label: str) -> Evidence:
    """`restore.json` — таблица рамки блокера приложения: что осталось и как вернуть."""
    attempts = {a.change.id: a for a in outcome.attempts}
    left = [
        {
            "параметр": f"ONVIF {name}",
            "где": "ONVIF Imaging / Media",
            "было": was,
            "осталось": now,
            "как вернуть": f"по ONVIF: {name} = {was}",
        }
        for name, was, now in outcome.left
    ]
    for change in outcome.native_left:
        attempt = attempts.get(change.id)
        write = attempt.write if attempt else None
        left.append(
            {
                "параметр": f"{label} {change.id}",
                "где": f"{write.method} {write.path}" if write else "",
                "было": change.before,
                "осталось": change.after,
                "как вернуть": attempt.how()
                if attempt
                else "веб-интерфейс камеры, раздел изображения",
            }
        )
    data = {"как вышло": outcome.stop.text(label) if outcome.stop else "", "осталось": left}
    return Evidence("restore.json", json.dumps(data, ensure_ascii=False, indent=1))


def run_search(
    ctx: CheckContext,
    collector: Collector,
    m0: NativeMap,
    reader: Callable[[Collector], NativeReader],
    known: set[int],
    label: str,
    send: Send | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchReport:
    """Автопоиск на камере с галочкой «изменяющие».

    Args:
        ctx: контекст проверки.
        collector: сборщик бренда.
        m0: карта M0 этого прогона.
        reader: новый `NativeReader` для сборщика тем же входом (`HttpLogin` или сеанс).
        known: номера пар, уже известных по ручной таблице и библиотеке, — заново не ищутся.
        label: имя родного API в строках.
        send: отправка запроса возврата родным API тем же входом; `None` — только вручную.
        sleep: пауза (тесты подменяют).
    """
    report = SearchReport()
    soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
    try:
        services = service_urls(soap, ctx.camera)
        _profile, _uri, source = first_stream(soap, services)
        imaging = state0 = None
        if "imaging" in services and source:
            imaging = ImagingWriter(soap, services["imaging"], source, {s.key: s for s in SETTINGS})
            imaging.declared()
            state0 = imaging.read()
        encoder = EncoderWriter(soap, services)
        configs = encoder.configs()
        config0 = configs[0] if configs else None
        options = encoder.options(config0.token) if config0 else {}
    except (CameraUnreachable, OnvifFault) as error:
        result = CheckResult.stand_error(f"ONVIF: настройки для записи не прочитаны: {error}")
        report.rows.append(_row(SEARCH_ID, "Автопоиск соответствий", result, Severity.MINOR))
        return report

    def now_of(entry: PendingWrite) -> str:
        item = BY_N.get(int(entry.kind)) if entry.kind.isdigit() else None
        if item is None:
            return ""
        if item.cls == "картинка" and imaging is not None:
            return str(imaging.read_value(item.setting))
        if config0 is not None:
            settings = encoder.read(config0.token).settings
            return str(
                settings.kbps if item.setting == "kbps" else (settings.encoding or "").upper()
            )
        return ""

    def put_back(entry: PendingWrite) -> str:
        restore = entry.restore
        if restore.get("service") == "imaging" and imaging is not None:
            imaging.restore_xml(restore.get("xml", ""))
        elif config0 is not None:
            encoder.restore_xml(
                restore.get("service", ""), restore.get("token", ""), restore.get("xml", "")
            )
        return now_of(entry)

    writes = WritePass(ctx, API)
    old = load(writes.folder, ctx.camera.address, ctx.camera.port) if writes.folder else None
    if old is not None and old.run_id == ctx.extra.get(RUN_ID_KEY, ""):
        old = None
    old_native = old_native_lines(old.restore, label) if old is not None else []
    try:
        writes.settle_old(now_of, put_back, (ImageWriteRefused, EncoderWriteRefused))
    except (CameraUnreachable, OnvifFault) as error:
        result = CheckResult.stand_error(f"журнал прошлого прогона не разобран: {error}")
        report.rows.append(_row(SEARCH_ID, "Автопоиск соответствий", result, Severity.MINOR))
        return report
    if old_native:
        # родным API журнал прошлого прогона не возвращается (restore.py): только вручную
        spec = API.lost_spec
        result = CheckResult(
            Status.FAILED,
            reason="прошлый прогон оставил параметры родного API изменёнными — верните вручную",
            expected="параметры родного API, сдвинутые прошлым прогоном, — как до записи",
            actual="; ".join(old_native),
        )
        writes.rows.append(
            ChildResult(spec.id, spec.title, result, spec.description, Severity.BLOCKER)
        )
    report.rows.extend(writes.rows)
    if any(r.result.status is Status.FAILED for r in writes.rows):
        result = CheckResult.skipped("прошлый прогон оставил камеру изменённой — поиск не начат")
        report.rows.append(_row(SEARCH_ID, "Автопоиск соответствий", result, Severity.MINOR))
        return report

    observed = Observed(collector, m0.sections, set(collector.observed(m0)))

    def snapshot() -> NativeMap:
        return reader(observed).read(NativeMap(brand=m0.brand, collector=m0.collector))

    ctx.progress("Автопоиск · исходная карта, 2 снимка")
    m0a = snapshot()
    sleep(limit(ctx, "api.baseline_gap_s").value)
    m0b = snapshot()
    volatile = volatile_ids(m0a, m0b)
    knobs = knobs_for(imaging, state0, encoder, config0, options, known)
    if not knobs:
        result = CheckResult.skipped("нечего искать: пары всех настроек уже известны")
        report.rows.append(_row(SEARCH_ID, "Автопоиск соответствий", result, Severity.MINOR))
        return report

    last: list = []

    def note(entry: Entry, old: str, new: str, stage: str, restore: dict[str, str]) -> None:
        last[:] = [entry, old, new, restore]
        writes.note(restore.get("service", ""), entry.title, str(entry.n), old, new, stage, restore)

    def journal_native(items: list[dict[str, str]]) -> None:
        entry, old_value, new_value, restore = last
        restore = {**restore, "native": json.dumps(items, ensure_ascii=False)}
        note(entry, old_value, new_value, "restoring", restore)

    restorer = NativeRestorer(collector, m0, volatile, send, journal_native)

    limits = Limits(
        apply_delay_s=limit(ctx, "api.apply_delay_s").value,
        share_low=limit(ctx, "api.share_low").value,
        share_high=limit(ctx, "api.share_high").value,
        residual_pct=limit(ctx, "api.scale_residual_pct").value,
        clamp_pct=limit(ctx, "api.clamp_tol_pct").value,
        budget_s=limit(ctx, "api.search_budget_s").value,
    )
    source_text = f"найдено автоматически, прогон {ctx.extra.get(RUN_ID_KEY, '')}".strip()
    search = Search(
        knobs,
        snapshot,
        m0b,
        volatile,
        collector.expected,
        note,
        limits,
        source_text,
        label,
        sleep=sleep,
        progress=ctx.progress,
        restorer=restorer.run,
    )
    outcome = search.run()
    restored = not outcome.left and not outcome.native_left
    if restored:
        writes.close()
    trace = Evidence("search.json", search.trace_json())
    # трасса — доказательство обеих строк: без него «прошла» не засчитывается (never.md)
    for row in (_search_row(outcome, label, restored), _restore_row(outcome, label)):
        row.result.evidence.append(trace)
        report.rows.append(row)
    report.rows[-1].result.evidence.append(_restore_facts(outcome, label))
    report.pairs, report.held = outcome.pairs, outcome.held
    report.evidence.append(trace)
    return report
