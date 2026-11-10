"""API-MAP-1: карта родного API камеры и сверка с ONVIF (S1, S2, S3, S6, S8, S9).

Проход без записи: паспорт и бренд → вход в родной API (одна попытка на камеру и прогон) → карта M0
→ ONVIF state0 → сверка по таблице пар → строки. Автопоиск с записью (S4, S5) — с галочкой
«изменяющие», отдельной задачей; без неё пары берутся из ручных таблиц (и библиотеки).

Идёт в сетевой фазе после `WEB-WALK-1` и `API-RUVER-1` (order 950): трафик обхода уже собран,
сеанс «Рувер» закрыт. Каждый запрос к камере — GET через `NativeReader` (табу, пределы); родной API
ничего не пишет.
"""

import datetime
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from qa_tool.checks.api.catalog import manual_pairs
from qa_tool.checks.api.compare import Tolerances, compare_pairs
from qa_tool.checks.api.library import LibraryPairs, load_library, resolve, save
from qa_tool.checks.api.limits import bad_thresholds, limit, timeout_s
from qa_tool.checks.api.onvif_state import State0, read_state0
from qa_tool.checks.api.restore import Send
from qa_tool.checks.api.search_run import SearchReport, run_search
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.checks.onvif.stream import first_stream
from qa_tool.checks.rtsp_refusal import refused_password, remember_native_refusal
from qa_tool.core.api_map import (
    LIBRARY_KEY,
    MAP_FILE,
    PAIRS_FILE,
    WEB_TRAFFIC_KEY,
    MapFormatError,
    NativeMap,
    PairTable,
    dumps_map,
    dumps_pairs,
)
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Status,
)
from qa_tool.core.secrets import redact
from qa_tool.core.write_journal import RUN_ID_KEY
from qa_tool.drivers.camera.collectors import all_collectors
from qa_tool.drivers.camera.digest import LoginUnsupported
from qa_tool.drivers.camera.http import HttpLogin, LoginRefused, TransportError, request
from qa_tool.drivers.camera.native import (
    MAX_RESPONSE_BYTES,
    Collector,
    NativeReader,
    NativeUnreachable,
    NativeWrite,
    choose_collector,
)
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.ruver.client import (
    LoginFailed,
    LoginRejected,
    NoFreeSession,
    RuverClient,
)
from qa_tool.drivers.camera.ruver.collector import ruver_fetch, ruver_send
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient
from qa_tool.drivers.camera.traffic import from_traffic

CHECK_ID = "API-MAP-1"
NOT_FOUND = "родной API не найден — нет сборщика для бренда и трафика веб-обхода"
EXCHANGES_LIMIT = 1 << 20
# имя родного API в строках протокола по сборщику
LABELS = {"ISAPI": "ISAPI", "CGI": "CGI", "OpenAPI": "API «Рувер»"}
ACCESS_ERRORS = (
    LoginRefused,
    LoginRejected,
    LoginUnsupported,
    LoginFailed,
    NoFreeSession,
    NativeUnreachable,
    TransportError,
)


def _passport(ctx: CheckContext) -> dict:
    """Паспорт ONVIF из `CORE-CONNECT-1`; нет — пустой."""
    passport = ctx.extra.get(PASSPORT_KEY)
    return passport if isinstance(passport, dict) else {}


def _meta(ctx: CheckContext, collector: Collector, passport: dict) -> NativeMap:
    """Заготовка карты: бренд, сборщик, паспорт, прогон, место, время."""
    return NativeMap(
        brand=collector.brand,
        collector=collector.name,
        collector_version=collector.version,
        maker=str(passport.get("manufacturer", "")),
        model=str(passport.get("model", "")),
        firmware=str(passport.get("firmware", "")),
        run=str(ctx.extra.get(RUN_ID_KEY, "")),
        place=ctx.camera.slot,
        time=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    )


def _exchanges(reader: NativeReader, ctx: CheckContext) -> Evidence:
    """`exchanges.txt`: запрос, код, размер и тело (у чувствительных разделов тела нет)."""
    parts = []
    for item in reader.exchanges:
        parts.append(f"{item.request}\nHTTP {item.code}, {item.size} байт\n{item.body}\n")
    text = redact("\n".join(parts), [ctx.password] if ctx.password else None)
    return Evidence("exchanges.txt", text[:EXCHANGES_LIMIT])


class Discovery:
    """Один проход `API-MAP-1` по одной камере; не потокобезопасен, как и рабочий процесс камеры."""

    def __init__(self, ctx: CheckContext) -> None:
        """Подготовить проход по контексту камеры."""
        self.ctx = ctx
        self.reader: NativeReader | None = None
        self.access: dict = {}
        self.send: Send | None = None

    def _reader(self, collector: Collector) -> NativeReader:
        """Новый читатель с общим для прохода входом (`HttpLogin` или сеанс «Рувер»)."""
        ctx = self.ctx
        return NativeReader(
            ctx.camera,
            collector,
            timeout_s=limit(ctx, "api.request_timeout_s").value,
            budget_s=limit(ctx, "api.map_budget_s").value,
            **self.access,
        )

    def open(self, collector: Collector, stack: ExitStack, meta: NativeMap) -> None:
        """Вход в родной API на весь проход: карта и снимки автопоиска идут одним входом.

        Raises:
            LoginRejected, LoginFailed, NoFreeSession, TransportError: сеанс «Рувер» не открыт.
        """
        ctx = self.ctx
        if collector.login == "session":
            client = RuverClient(
                ctx.camera.address,
                ctx.camera.port,
                ctx.camera.username,
                ctx.password,
                # запись — только возврат S5 (`ruver_send`); чтение карты идёт только операциями
                # чтения (`ruver_fetch`), галочка его не расширяет
                allow_writes=ctx.allow_writes,
                timeout=limit(ctx, "api.request_timeout_s").value,
            )
            stack.enter_context(client)
            meta.login = "сеанс «Рувер»"
            self.access = {"login": None, "fetch": ruver_fetch(client, collector)}
            self.send = ruver_send(client, collector)
            return
        login = HttpLogin(
            ctx.camera.username,
            ctx.password,
            already_refused=refused_password(ctx),
            on_refused=lambda reason: remember_native_refusal(ctx, reason),
        )
        self.access = {"login": login}
        self.send = self._http_send(login)

    def _http_send(self, login: HttpLogin) -> Send:
        """Отправка возврата S5 тем же входом Digest; табу и слова-признаки — в `http.request`."""
        ctx = self.ctx

        def send(write: NativeWrite) -> int:
            query = f"?{write.query}" if write.query else ""
            url = f"http://{ctx.camera.address}:{ctx.camera.port}{write.path}{query}"
            headers = {"Content-Type": "application/xml"} if write.body.startswith("<") else {}
            response = request(
                write.method,
                url,
                content=write.body,
                headers=headers,
                timeout=limit(ctx, "api.request_timeout_s").value,
                login=login,
                max_bytes=MAX_RESPONSE_BYTES,
            )
            return response.status_code

        return send

    def state0(self) -> State0:
        """ONVIF до записи; не прочиталось — листы без значений, сверка их пропустит."""
        ctx = self.ctx
        soap = SoapClient(ctx.camera.username, ctx.password, timeout=10)
        try:
            services = service_urls(soap, ctx.camera)
            _profile, _uri, source = first_stream(soap, services)
        except (CameraUnreachable, OnvifFault) as error:
            return State0(problems=[f"ONVIF: службы камеры не прочитаны: {error}"])
        return read_state0(soap, services, source)

    def run(self) -> CheckResult:
        """Пройти карту, автопоиск (с галочкой) и сверку; вернуть составной результат."""
        ctx = self.ctx
        refused = refused_password(ctx)
        if refused:
            return CheckResult.stand_error(f"вход в родной API не пробовали: {refused}")
        bad = bad_thresholds(ctx)
        if bad:
            return CheckResult.stand_error(f"проверьте thresholds.yaml: {bad}")
        passport = _passport(ctx)
        choice = choose_collector(all_collectors(), passport, ctx.camera)
        collector = choice.collector
        if collector is None:
            return self._traffic_map(passport, "")
        why = getattr(collector, "why", "")
        if why:
            return self._traffic_map(passport, why)
        ctx.progress(f"карта родного API: {collector.name}")
        meta = _meta(ctx, collector, passport)
        with ExitStack() as stack:
            try:
                self.open(collector, stack, meta)
                self.reader = self._reader(collector)
                native = self.reader.read(meta)
                if not any(s.code == 200 for s in native.sections):
                    return self._traffic_map(passport, f"сборщик {collector.name}: разделов нет")
                return self._result(collector, native, choice.note)
            except ACCESS_ERRORS as error:
                return self._access_failed(error)

    def _traffic_map(self, passport: dict, why: str) -> CheckResult:
        """Карта по трафику веб-обхода этого прогона (S2): без сверки и автопоиска.

        Args:
            passport: паспорт ONVIF камеры.
            why: почему родной API не читается напрямую (заготовка бренда, нет разделов); пусто —
                для бренда нет сборщика.
        """
        ctx = self.ctx
        lead = why or NOT_FOUND
        traffic = ctx.extra.get(WEB_TRAFFIC_KEY)
        if not traffic:
            tail = "проверка «Веб-интерфейс» не входила в сценарий или не прошла страницу входа"
            if why:
                return CheckResult.skipped(f"{why}; трафика веб-обхода нет: {tail}")
            return CheckResult.skipped(f"{NOT_FOUND}: {tail}")
        ctx.progress(f"карта API по трафику веб-обхода: запросов {len(traffic['запросы'])}")
        meta = NativeMap(
            brand=str(passport.get("manufacturer", "")).lower()[:64],
            collector="трафик",
            maker=str(passport.get("manufacturer", "")),
            model=str(passport.get("model", "")),
            firmware=str(passport.get("firmware", "")),
            run=str(ctx.extra.get(RUN_ID_KEY, "")),
            place=ctx.camera.slot,
            time=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            login="вход веб-обхода",
        )
        native = from_traffic(list(traffic["запросы"]), list(traffic["остановлены"]), meta)
        if not native.sections:
            return CheckResult.skipped(
                f"{lead}; веб-обход не увидел запросов страницы к данным камеры "
                f"(запросов {len(traffic['запросы'])})"
            )
        notes = [n for n in (why, native.cut_reason) if n]
        evidence = []
        try:
            evidence.append(Evidence(MAP_FILE, dumps_map(native)))
        except MapFormatError as error:
            return CheckResult.stand_error(f"карта по трафику не записана: {error}")
        actual = (
            f"карта по трафику веб-обхода: разделов {len(native.sections)}, "
            f"параметров {native.param_count()}; допустимые значения не видны, сверки нет"
        )
        if notes:
            actual += "; " + "; ".join(notes)
        return CheckResult(
            Status.PASSED,
            expected="родной API виден в трафике веб-интерфейса, карта снята",
            actual=actual,
            evidence=evidence,
            data={"native_sections": len(native.sections), "native_params": native.param_count()},
        )

    def _access_failed(self, error: Exception) -> CheckResult:
        """Строка при отказе входа, неподдержанном входе или пропаже камеры (S8)."""
        if isinstance(error, LoginRejected):
            remember_native_refusal(self.ctx, str(error))
        if isinstance(error, LoginRefused | LoginRejected):
            return CheckResult.stand_error(f"вход в родной API отвергнут: {error}")
        if isinstance(error, LoginUnsupported):
            if "Basic" in str(error):
                return CheckResult.skipped(f"пароль ушёл бы открытым текстом: {error}")
            return CheckResult.stand_error(f"способ входа не поддержан: {error}")
        if isinstance(error, LoginFailed | NoFreeSession):
            return CheckResult.stand_error(f"вход в родной API не удался: {error}")
        if isinstance(error, NativeUnreachable):
            return CheckResult.stand_error(f"камера пропала во время чтения карты: {error}")
        return CheckResult.stand_error(f"камера не ответила: {type(error).__name__}")

    def _result(self, collector: Collector, native: NativeMap, note: str) -> CheckResult:
        """Строка API-MAP-1, строки сверки, доказательства."""
        ctx = self.ctx
        ctx.progress("сверка с ONVIF")
        state = self.state0()
        manual = manual_pairs(collector.brand, native.model, native.firmware)
        root = self.ctx.extra.get(LIBRARY_KEY)
        library = (
            load_library(Path(root), collector.brand, native.model, native.firmware)
            if root
            else LibraryPairs()
        )
        label = LABELS.get(collector.name, collector.name)
        found = SearchReport()
        if ctx.allow_writes:
            known = {p.n for p in resolve([], list(manual.pairs), library)}
            found = run_search(ctx, collector, native, self._reader, known, label, self.send)
        pairs = resolve(found.pairs, list(manual.pairs), library)
        for n, (held, held_note) in found.held.items():
            if n in state.sides:
                state.sides[n] = replace(state.sides[n], held=held, held_note=held_note)
        tol = Tolerances(
            limit(ctx, "api.compare_tol_pct").value, limit(ctx, "api.range_tol_pct").value
        )
        children: list[ChildResult] = [
            *found.rows,
            *compare_pairs(native, state.sides, pairs, tol, label),
        ]
        notes = [n for n in (note, native.cut_reason, manual.problem, *state.problems) if n]
        notes += library.problems
        if root:
            table = PairTable(native.maker, native.model, native.firmware, found.pairs)
            notes += save(Path(root), native, table)
        evidence = list(found.evidence)
        if found.pairs:
            table = PairTable(native.maker, native.model, native.firmware, found.pairs)
            evidence.append(Evidence(PAIRS_FILE, dumps_pairs(table)))
        try:
            evidence.append(Evidence(MAP_FILE, dumps_map(native)))
        except MapFormatError as error:
            notes.append(f"api-map.json не записан: {error}")
        if self.reader is not None:
            evidence.append(_exchanges(self.reader, ctx))
        actual = (
            f"карта снята: {label}, разделов {len(native.sections)}, "
            f"параметров {native.param_count()}, ошибок разделов {len(native.errors)}"
        )
        if notes:
            actual += "; " + "; ".join(notes)
        return CheckResult(
            Status.PASSED,
            expected="родной API читается, карта снята; одна настройка в родном API и ONVIF "
            "читается одинаково",
            actual=actual,
            evidence=evidence,
            children=children,
            data={"native_sections": len(native.sections), "native_params": native.param_count()},
        )


@REGISTRY.check(
    CHECK_ID,
    module="api",
    title="API камеры: карта и сверка с ONVIF",
    description="Программа читает родной API камеры (Hikvision ISAPI, Dahua CGI, API «Рувер») "
    "только запросами чтения и снимает карту: разделы, параметры, допустимые и текущие "
    "значения. Затем сверяет настройки, которые есть и в родном API, и в ONVIF: одна "
    "настройка должна читаться одинаково. Значения сети, учётных записей и безопасности "
    "не сохраняются.",
    phase=Phase.NETWORK,
    order=950,
    writes=False,
    timeout_s=timeout_s(),
    help_id="api-map",
    read_only_note=(
        "Без этой галочки «API камеры» только снимет карту родного API и сверит её с ONVIF "
        "по найденным раньше и ручным соответствиям: новые соответствия не ищутся."
    ),
    writes_note=(
        "«API камеры» будет менять настройки изображения, битрейт и кодек по ONVIF, чтобы найти "
        "соответствия с родным API, и возвращать каждую после проверки. Сеть и пароли не меняются."
    ),
)
def api_map(ctx: CheckContext) -> CheckResult:
    """Карта родного API и сверка с ONVIF; составной результат со строками пар."""
    return Discovery(ctx).run()
