"""API-MAP-1: карта родного API камеры и сверка с ONVIF (S1, S2, S3, S6, S8, S9).

Проход без записи: паспорт и бренд → вход в родной API (одна попытка на камеру и прогон) → карта M0
→ ONVIF state0 → сверка по таблице пар → строки. Автопоиск с записью (S4, S5) — с галочкой
«изменяющие», отдельной задачей; без неё пары берутся из ручных таблиц (и библиотеки).

Идёт в сетевой фазе после `WEB-WALK-1` и `API-RUVER-1` (order 950): трафик обхода уже собран,
сеанс «Рувер» закрыт. Каждый запрос к камере — GET через `NativeReader` (табу, пределы); родной API
ничего не пишет.
"""

import datetime

from qa_tool.checks.api.catalog import manual_pairs
from qa_tool.checks.api.compare import Tolerances, compare_pairs
from qa_tool.checks.api.limits import bad_thresholds, limit, timeout_s
from qa_tool.checks.api.onvif_state import State0, read_state0
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.checks.onvif.stream import first_stream
from qa_tool.checks.rtsp_refusal import refused_password, remember_native_refusal
from qa_tool.core.api_map import MAP_FILE, MapFormatError, NativeMap, dumps_map
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
from qa_tool.drivers.camera.http import HttpLogin, LoginRefused, TransportError
from qa_tool.drivers.camera.native import (
    Collector,
    NativeReader,
    NativeUnreachable,
    choose_collector,
)
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.ruver.client import (
    LoginFailed,
    LoginRejected,
    NoFreeSession,
    RuverClient,
)
from qa_tool.drivers.camera.ruver.collector import ruver_fetch
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient

CHECK_ID = "API-MAP-1"
NOT_FOUND = "родной API не найден — нет сборщика для бренда и трафика веб-обхода"
EXCHANGES_LIMIT = 1 << 20
# имя родного API в строках протокола по сборщику
LABELS = {"ISAPI": "ISAPI", "CGI": "CGI", "OpenAPI": "API «Рувер»"}


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

    def _reader(self, collector: Collector, **kwargs) -> NativeReader:
        ctx = self.ctx
        self.reader = NativeReader(
            ctx.camera,
            collector,
            timeout_s=limit(ctx, "api.request_timeout_s").value,
            budget_s=limit(ctx, "api.map_budget_s").value,
            **kwargs,
        )
        return self.reader

    def read_map(self, collector: Collector, meta: NativeMap) -> NativeMap:
        """Снять карту сборщиком своим способом входа.

        Raises:
            LoginRefused, LoginUnsupported, LoginRejected, LoginFailed, NoFreeSession,
            NativeUnreachable, TransportError: см. `run`.
        """
        ctx = self.ctx
        if collector.login == "session":
            client = RuverClient(
                ctx.camera.address,
                ctx.camera.port,
                ctx.camera.username,
                ctx.password,
                timeout=limit(ctx, "api.request_timeout_s").value,
            )
            with client:
                meta.login = "сеанс «Рувер»"
                return self._reader(
                    collector, login=None, fetch=ruver_fetch(client, collector)
                ).read(meta)
        login = HttpLogin(
            ctx.camera.username,
            ctx.password,
            already_refused=refused_password(ctx),
            on_refused=lambda reason: remember_native_refusal(ctx, reason),
        )
        return self._reader(collector, login=login).read(meta)

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
        """Пройти карту и сверку; вернуть составной результат."""
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
            return CheckResult.skipped(NOT_FOUND)
        why = getattr(collector, "why", "")
        if why:
            return CheckResult.skipped(f"{why}; трафика веб-обхода нет")
        ctx.progress(f"карта родного API: {collector.name}")
        meta = _meta(ctx, collector, passport)
        try:
            native = self.read_map(collector, meta)
        except (LoginRefused, LoginRejected) as error:
            if isinstance(error, LoginRejected):
                remember_native_refusal(ctx, str(error))
            return CheckResult.stand_error(f"вход в родной API отвергнут: {error}")
        except LoginUnsupported as error:
            if "Basic" in str(error):
                return CheckResult.skipped(f"пароль ушёл бы открытым текстом: {error}")
            return CheckResult.stand_error(f"способ входа не поддержан: {error}")
        except (LoginFailed, NoFreeSession) as error:
            return CheckResult.stand_error(f"вход в родной API не удался: {error}")
        except NativeUnreachable as error:
            return CheckResult.stand_error(f"камера пропала во время чтения карты: {error}")
        except TransportError as error:
            return CheckResult.stand_error(f"камера не ответила на вход: {type(error).__name__}")
        if not any(s.code == 200 for s in native.sections):
            return CheckResult.skipped(f"{NOT_FOUND} (сборщик {collector.name}: разделов нет)")
        return self._result(collector, native, choice.note)

    def _result(self, collector: Collector, native: NativeMap, note: str) -> CheckResult:
        """Строка API-MAP-1, строки сверки, доказательства."""
        ctx = self.ctx
        ctx.progress("сверка с ONVIF")
        state = self.state0()
        manual = manual_pairs(collector.brand, native.model, native.firmware)
        tol = Tolerances(
            limit(ctx, "api.compare_tol_pct").value, limit(ctx, "api.range_tol_pct").value
        )
        label = LABELS.get(collector.name, collector.name)
        children: list[ChildResult] = compare_pairs(
            native, state.sides, list(manual.pairs), tol, label
        )
        notes = [n for n in (note, native.cut_reason, manual.problem, *state.problems) if n]
        evidence = []
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
)
def api_map(ctx: CheckContext) -> CheckResult:
    """Карта родного API и сверка с ONVIF; составной результат со строками пар."""
    return Discovery(ctx).run()
