"""API-RUVER-1: все операции API камеры «Рувер» по карте, строка протокола на операцию.

Одна составная проверка на камеру, а не проверка на раздел: сеанс и исходные значения живут в
одном контексте, и `finally` выполняется всегда. Планировщик снимает процесс только по
`timeout_s`, поэтому у обхода свой предел времени меньше него: при пределе и при остановке новые
значения не пишутся, а записанное возвращается и сверяется до выхода (AD-3).

Порядок: чтение всех настроек → запись с возвратом → сверка обходом чтения → разрушающие,
перезагрузка последней. Первое чтение одновременно служит снимком «до».

Камера не меняет сеть и учётные записи: такие операции закрыты табу в `core/taboo.py` и картой
(AD-7). Операции вне карты клиент не вызовет вовсе.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from jsonschema import Draft7Validator

from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Status,
)
from qa_tool.core.models import RUVER
from qa_tool.core.secrets import redact
from qa_tool.core.worker import STOPPED
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.ruver.client import (
    DESTRUCTIVE,
    NOT_CALLED,
    READ,
    TABOO,
    WRITE,
    ApiMap,
    LoginFailed,
    LoginRejected,
    NoFreeSession,
    Operation,
    RuverClient,
    RuverError,
    load_map,
)

CHECK_ID = "API-RUVER-1"
RESTORE_ID = "API-RUVER-RESTORE"
NEED_REBOOT = "/api/service/need_reboot"
INFO = "/api/info"
REBOOT = "/api/service/reboot"
LEFT_CHANGED = "камера осталась изменённой"
PER_OPERATION_S = 3.0  # обычно 0,1 с; запас на медленную камеру и тайм-ауты отдельных запросов
RESERVE_S = 300.0  # возврат значений и сверка уже после предела времени
REBOOT_WAIT_S = 180.0  # камера «Рувер» поднимается за 1–2 минуты
REBOOT_POLL_S = 3.0
EVIDENCE_LIMIT = 20_000  # ответы API невелики; ограничение — от чужой прошивки с большим ответом


def budget_s(api: ApiMap) -> float:
    """Предел времени обхода по размеру карты, с."""
    return len(api.operations) * PER_OPERATION_S


def fingerprint(content: bytes) -> str:
    """Размер и хеш вместо содержимого: для двоичных ответов и ответов с возможными секретами."""
    return f"размер {len(content)} байт, sha256 {hashlib.sha256(content).hexdigest()}"


def schema_errors(schema: dict, data: Any, sensitive: bool) -> list[str]:
    """Расхождения ответа со схемой. Для закрытых ответов — без значений из ответа."""
    found = []
    for error in Draft7Validator(schema).iter_errors(data):
        where = "/".join(str(part) for part in error.absolute_path) or "ответ"
        found.append(f"{where}: {error.validator}" if sensitive else f"{where}: {error.message}")
    return found


def other_value(op: Operation, current: Any, limits: Any) -> Any:
    """Другое допустимое значение настройки; None — такого нет."""
    if op.boolean:
        return not current if isinstance(current, bool) else None
    if op.values:
        return next((value for value in op.values if value != current), None)
    if not isinstance(limits, dict) or not isinstance(current, int):
        return None
    low, high = limits.get("min"), limits.get("max")
    if not isinstance(low, int) or not isinstance(high, int) or low >= high:
        return None
    return current + 1 if current < high else current - 1


def beyond_limits(op: Operation, limits: Any) -> int | None:
    """Значение вне пределов, заданных описанием или ответом камеры; None — пределов нет."""
    if op.values and all(isinstance(value, int) for value in op.values):
        return max(op.values) + 1
    if isinstance(limits, dict) and isinstance(limits.get("max"), int):
        return limits["max"] + 1
    return None


def changed_fields(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Имена разошедшихся полей двух ответов; значения намеренно не возвращаются."""
    if isinstance(before, dict) and isinstance(after, dict):
        names = sorted(set(before) | set(after))
        return [
            found
            for name in names
            for found in changed_fields(before.get(name), after.get(name), f"{prefix}{name}.")
        ]
    return [] if before == after else [prefix.rstrip(".") or "ответ"]


@dataclass
class Walk:
    """Один обход API камеры; живёт внутри открытого сеанса `RuverClient`."""

    ctx: CheckContext
    client: RuverClient
    api: ApiMap
    deadline: float
    hard_deadline: float
    rows: dict[str, ChildResult] = field(default_factory=dict)
    before: dict[str, Any] = field(default_factory=dict)  # снимок «до»: только в памяти
    firmware: str = ""
    wrote: bool = False  # что-то могло измениться: нужна сверка «до и после»

    # --- общее ------------------------------------------------------------------------------

    def _halted(self) -> str:
        """Почему нельзя начинать новую операцию; пусто — можно."""
        if self.ctx.stop_requested():
            return STOPPED
        if time.monotonic() > self.deadline:
            return "проверка API не уложилась в предел времени"
        return ""

    def _row(self, op: Operation, result: CheckResult, suffix: str = "") -> None:
        """Записать строку протокола; описание — метод, адрес и вид операции."""
        row_id = op.id + suffix
        self.rows[row_id] = ChildResult(
            row_id,
            f"{op.method} {op.path}: {op.title}" + (" (вне пределов)" if suffix else ""),
            result,
            description=f"Операция API «Рувер» {op.method} {op.path}, вид «{op.kind}».",
        )

    def _clean(self, text: str) -> str:
        return redact(text, [self.ctx.password, self.client.session_id])

    def _evidence(self, op: Operation, response, sent: Any = None) -> Evidence:
        """Запрос и ответ; двоичный ответ и ответ с возможными секретами — только размер и хеш."""
        request = f"{op.method} {op.path}" + (f"?{urlencode(op.query)}" if op.query else "")
        if sent is not None:
            request += f"\nтело: {json.dumps(sent, ensure_ascii=False)}"
        if op.binary or op.sensitive:
            shown = fingerprint(response.content)
        else:
            shown = response.text[:EVIDENCE_LIMIT]
        text = f"{request}\n\nкод {response.status_code}\n{shown}"
        return Evidence(f"{op.id}.txt", self._clean(text))

    def _refused(self, op: Operation, response) -> CheckResult | None:
        """Итог для ответов, которые не говорят о качестве операции; None — ответ разбирать."""
        code = response.status_code
        if code == 404:
            where = self.firmware or "этой камеры"
            return CheckResult.skipped(f"нет в прошивке {where}")
        if code in (401, 403):
            result = CheckResult.stand_error(f"камера отказала в доступе (код {code})")
            result.evidence = [self._evidence(op, response)]
            return result
        return None

    # --- чтение -----------------------------------------------------------------------------

    def read_all(self) -> None:
        """Вызвать каждую операцию чтения один раз и запомнить снимок «до».

        Адрес со сведениями о камере идёт первым: версия прошивки нужна в причинах 404.
        """
        reads = [op for op in self.api.operations if op.kind == READ]
        for op in sorted(reads, key=lambda op: op.path != INFO):
            self._row(op, self._read(op))

    def _read(self, op: Operation) -> CheckResult:
        if op.missing:
            return CheckResult.skipped(f"нужны параметры: {', '.join(op.missing)}")
        halted = self._halted()
        if halted:
            return CheckResult.skipped(halted)
        try:
            response = self.client.call(op)
        except http.TransportError as e:
            return CheckResult.stand_error(f"камера не ответила: {type(e).__name__}")
        refused = self._refused(op, response)
        if refused:
            return refused
        evidence = [self._evidence(op, response)]
        expected = "ответ 200 и совпадение со схемой описания"
        if response.status_code != 200:
            return self._failed(f"код {response.status_code}", expected, evidence)
        if op.binary:
            if not response.content:
                return self._failed("пустой ответ", expected, evidence)
            return CheckResult(
                Status.PASSED,
                expected=expected,
                actual=fingerprint(response.content),
                evidence=evidence,
            )
        if op.schema is None:
            return CheckResult(
                Status.PASSED, expected=expected, actual="код 200", evidence=evidence
            )
        try:
            data = response.json()
        except ValueError:
            return self._failed("ответ не в формате JSON", expected, evidence)
        errors = schema_errors(op.schema, data, op.sensitive)
        if errors:
            return self._failed(
                self._clean("ответ не совпал со схемой: " + "; ".join(errors[:5]))[:500],
                expected,
                evidence,
            )
        self.before[op.id] = data
        if op.path == INFO and isinstance(data, dict):
            self.firmware = str(data.get("firmwareVersion") or "")
        return CheckResult(
            Status.PASSED, expected=expected, actual="совпадает со схемой", evidence=evidence
        )

    @staticmethod
    def _failed(reason: str, expected: str, evidence: list[Evidence]) -> CheckResult:
        return CheckResult(
            Status.FAILED, reason=reason, expected=expected, actual=reason, evidence=evidence
        )

    # --- запись -----------------------------------------------------------------------------

    def _read_field(self, read_op: Operation, name: str) -> tuple[Any, Any]:
        """(значение поля, весь ответ); камера не дала поле — ValueError."""
        response = self.client.call(read_op)
        if response.status_code != 200:
            raise ValueError(f"чтение вернуло код {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or name not in data:
            raise ValueError(f"в ответе чтения нет поля {name}")
        return data[name], data

    def _needs_reboot(self, op: Operation) -> bool | None:
        """Нужна ли настройке перезагрузка; None — камера не ответила понятно."""
        check = self.api.get("POST", NEED_REBOOT)
        response = self.client.call(check, {"route": op.path})
        if response.status_code != 200:
            return None
        try:
            answer = response.json().get("needReboot")
        except (ValueError, AttributeError):
            return None
        return answer if isinstance(answer, bool) else None

    def write_all(self) -> None:
        """Записать и вернуть каждую настройку вида «запись»; строки с причинами остальным."""
        for op in self.api.operations:
            if op.kind != WRITE:
                continue
            if not self.ctx.allow_writes:
                self._row(op, CheckResult.skipped("не разрешено сценарием: изменяющая проверка"))
                continue
            halted = self._halted()
            if halted:
                self._row(op, CheckResult.skipped(halted))
                continue
            try:
                self._write(op)
            except http.TransportError as e:
                reason = f"камера не ответила: {type(e).__name__}"
                self._row(op, CheckResult.stand_error(reason))

    def _write(self, op: Operation) -> None:
        reboot = self._needs_reboot(op)
        if reboot is None:
            self._row(
                op, CheckResult.skipped("камера не ответила, нужна ли настройке перезагрузка")
            )
            return
        if reboot:
            self._row(op, CheckResult.skipped("применяется после перезагрузки"))
            return
        read_op = self.api.get("GET", op.path)
        try:
            original, data = self._read_field(read_op, op.setting)
        except ValueError as e:
            self._row(op, CheckResult.stand_error(f"значение не прочитано: {e}"))
            return
        limits = data.get(op.range_field) if op.range_field else None
        value = other_value(op, original, limits)
        if value is None:
            self._row(op, CheckResult.skipped("нет другого допустимого значения"))
            return
        self._row(op, self._change_and_return(op, read_op, original, value))
        outside = beyond_limits(op, limits)
        if outside is None or self._halted():
            return
        try:
            self._row(op, self._try_outside(op, read_op, original, outside), "-LIMIT")
        except http.TransportError as e:
            reason = f"камера не ответила: {type(e).__name__}"
            self._row(op, CheckResult.stand_error(reason), "-LIMIT")

    def _change_and_return(
        self, op: Operation, read_op: Operation, original: Any, value: Any
    ) -> CheckResult:
        """Записать значение, прочитать, вернуть исходное и сверить его (S6).

        Значение возвращается и после сбоя связи: ответ мог потеряться, а запись примениться.
        """
        expected = f"{op.setting}: записанное значение читается, затем исходное возвращается"
        sent = {op.setting: value}
        self.wrote = True
        reason, response = "", None
        try:
            response = self.client.call(op, sent)
            refused = self._refused(op, response)
            if refused:
                return refused
            if not response.is_success:
                reason = f"камера отказала в записи: код {response.status_code}"
                return self._failed(reason, expected, [self._evidence(op, response, sent)])
            read_back, _ = self._read_field(read_op, op.setting)
            if read_back != value:
                reason = f"записано {value}, прочитано {read_back}"
        except (ValueError, http.TransportError) as e:
            reason = f"значение не прочитано после записи: {type(e).__name__}: {e}"
        problem = self._return_value(op, read_op, original)
        evidence = [self._evidence(op, response, sent)] if response is not None else []
        if problem:
            return self._failed(f"{LEFT_CHANGED}: {problem}", expected, evidence)
        if reason and response is None:
            return CheckResult.stand_error(reason)
        if reason:
            return self._failed(reason, expected, evidence)
        return CheckResult(
            Status.PASSED,
            expected=expected,
            actual=f"записано и прочитано {value}, исходное {original} возвращено",
            evidence=evidence,
        )

    def _return_value(self, op: Operation, read_op: Operation, original: Any) -> str:
        """Вернуть исходное значение и сверить; пусто — вернулось, иначе что не так."""
        try:
            response = self.client.call(op, {op.setting: original})
            if not response.is_success:
                return f"возврат отказан: код {response.status_code}"
            current, _ = self._read_field(read_op, op.setting)
        except (ValueError, http.TransportError, RuverError) as e:
            return f"возврат не подтверждён: {type(e).__name__}"
        return "" if current == original else f"{op.setting} = {current}, было {original}"

    def _try_outside(
        self, op: Operation, read_op: Operation, original: Any, value: int
    ) -> CheckResult:
        """Отправить значение вне пределов: камера обязана отказать и не менять настройку (S7)."""
        expected = f"{op.setting} = {value} вне пределов: ошибка, значение не меняется"
        self.wrote = True
        response = self.client.call(op, {op.setting: value})
        evidence = [self._evidence(op, response, {op.setting: value})]
        try:
            current, _ = self._read_field(read_op, op.setting)
        except ValueError as e:
            return CheckResult.stand_error(f"значение не прочитано: {e}")
        problem = ""
        if current != original:
            problem = self._return_value(op, read_op, original)
        if problem:
            return self._failed(f"{LEFT_CHANGED}: {problem}", expected, evidence)
        if response.is_success:
            return self._failed(
                f"камера приняла значение вне пределов, прочитано {current}", expected, evidence
            )
        if current != original:
            return self._failed(
                f"камера отказала, но значение изменилось на {current}", expected, evidence
            )
        return CheckResult(
            Status.PASSED,
            expected=expected,
            actual=f"код {response.status_code}, значение осталось {original}",
            evidence=evidence,
        )

    # --- сверка «до и после» ----------------------------------------------------------------

    def _read_again(self) -> tuple[dict[str, list[str]], list[str]]:
        """Обход чтения: разошедшиеся поля по операциям и что сверить не удалось."""
        changed: dict[str, list[str]] = {}
        unread: list[str] = []
        for op in self.api.operations:
            if op.kind != READ or op.live or op.id not in self.before:
                continue
            if time.monotonic() > self.hard_deadline:
                unread.append(op.path)
                continue
            try:
                response = self.client.call(op)
                data = response.json() if response.status_code == 200 else None
            except (ValueError, http.TransportError):
                data = None
            if data is None:
                unread.append(op.path)
                continue
            fields = changed_fields(self.before[op.id], data)
            if fields:
                changed[op.path] = fields
        return changed, unread

    def _repair(self, changed: dict[str, list[str]]) -> None:
        """Ещё раз вернуть настройки вида «запись», которые разошлись со снимком «до»."""
        for path in changed:
            op, read_op = self.api.get("POST", path), self.api.get("GET", path)
            if op is None or op.kind != WRITE or read_op.id not in self.before:
                continue
            before = self.before[read_op.id]
            if op.setting in before:
                self._return_value(op, read_op, before[op.setting])

    def verify(self) -> None:
        """Строка сверки: настройки после записи совпали со снимком «до» (S6)."""
        changed, unread = self._read_again()
        if changed:
            self._repair(changed)
            changed, unread = self._read_again()
        lines = [f"{path}: {', '.join(fields)}" for path, fields in changed.items()]
        expected = "настройки после записи совпадают с обходом чтения до неё"
        text = "\n".join(["Разошлись:", *lines, "Не прочитаны:", *unread])
        evidence = [Evidence(f"{RESTORE_ID}.txt", self._clean(text))]
        if lines:
            result = self._failed(f"{LEFT_CHANGED}: " + "; ".join(lines)[:400], expected, evidence)
        elif unread:
            result = CheckResult.stand_error(f"сверка не закончена: {len(unread)} адресов")
            result.evidence = evidence
        else:
            result = CheckResult(
                Status.PASSED, expected=expected, actual="совпало", evidence=evidence
            )
        self.rows[RESTORE_ID] = ChildResult(
            RESTORE_ID, "Сверка настроек после записи через API", result
        )

    # --- разрушающие ------------------------------------------------------------------------

    def destroy_all(self) -> None:
        """Разрушающие операции; перезагрузка — последней, после неё сеанс новый."""
        operations = [op for op in self.api.operations if op.kind == DESTRUCTIVE]
        for op in sorted(operations, key=lambda op: op.path == REBOOT):
            if not self.ctx.allow_destructive:
                self._row(op, CheckResult.skipped("не разрешено сценарием: разрушающая проверка"))
            elif op.missing:
                self._row(op, CheckResult.skipped(f"нужны параметры: {', '.join(op.missing)}"))
            elif self._halted():
                self._row(op, CheckResult.skipped(self._halted()))
            else:
                self._row(op, self._destroy(op))

    def _destroy(self, op: Operation) -> CheckResult:
        expected = "камера выполнила операцию"
        try:
            response = self.client.call(op)
        except http.TransportError as e:
            return CheckResult.stand_error(f"камера не ответила: {type(e).__name__}")
        refused = self._refused(op, response)
        if refused:
            return refused
        evidence = [self._evidence(op, response)]
        if not response.is_success:
            return self._failed(f"код {response.status_code}", expected, evidence)
        if op.path == REBOOT and not self._came_back():
            return self._failed(f"камера не вернулась за {REBOOT_WAIT_S:g} с", expected, evidence)
        return CheckResult(Status.PASSED, expected=expected, actual="выполнено", evidence=evidence)

    def _came_back(self) -> bool:
        """Дождаться камеру после перезагрузки: чтение снова отвечает 200."""
        info = self.api.get("GET", INFO)
        limit = time.monotonic() + float(self.ctx.extra.get("api_reboot_wait_s", REBOOT_WAIT_S))
        while time.monotonic() < limit:
            try:
                if self.client.call(info).status_code == 200:
                    return True
            except http.TransportError:
                pass  # камера ещё поднимается
            time.sleep(float(self.ctx.extra.get("api_reboot_poll_s", REBOOT_POLL_S)))
        return False

    # --- итог -------------------------------------------------------------------------------

    def run(self) -> list[ChildResult]:
        """Весь обход; строки в порядке карты, сверка — последней."""
        self.read_all()
        self.write_all()
        if self.wrote:
            self.verify()
        self.destroy_all()
        return self.ordered()

    def ordered(self) -> list[ChildResult]:
        """Строки всех операций: и вызванных, и пропущенных по виду."""
        for op in self.api.operations:
            if op.id in self.rows:
                continue
            if op.kind == TABOO:
                self._row(op, CheckResult.skipped("запрещено табу"))
            elif op.kind == NOT_CALLED:
                self._row(op, CheckResult.skipped(f"не вызывается: {op.reason}"))
        order = [op.id for op in self.api.operations]
        rows = []
        for row_id in order:
            rows.append(self.rows[row_id])
            if row_id + "-LIMIT" in self.rows:
                rows.append(self.rows[row_id + "-LIMIT"])
        if RESTORE_ID in self.rows:
            rows.append(self.rows[RESTORE_ID])
        return rows


@REGISTRY.check(
    CHECK_ID,
    module="api",
    title="API «Рувер»",
    description="Каждая операция API камеры по карте: чтение — ответ по описанию; с галочкой "
    "«изменяющие» запись читается обратно и возвращается как была. Сеть, учётные записи и время "
    "не меняются никогда.",
    phase=Phase.NETWORK,
    order=900,
    vendors={RUVER},
    timeout_s=budget_s(load_map()) + RESERVE_S + REBOOT_WAIT_S,
    help_id="api-ruver",
)
def api_ruver(ctx: CheckContext) -> CheckResult:
    """Обойти API камеры в одном сеансе и вернуть строку протокола на каждую операцию.

    Вход не удался — одна строка `API-RUVER-1`: операции не вызывались, строка на каждую
    только повторила бы причину 300 раз.
    """
    api = load_map()
    start = time.monotonic()
    deadline = start + float(ctx.extra.get("api_budget_s", budget_s(api)))
    client = RuverClient(
        ctx.camera.address,
        ctx.camera.port,
        ctx.camera.username,
        ctx.password,
        api=api,
        allow_writes=ctx.allow_writes,
        allow_destructive=ctx.allow_destructive,
        timeout=float(ctx.extra.get("api_timeout_s", 5.0)),
    )
    try:
        with client:
            walk = Walk(ctx, client, api, deadline, deadline + RESERVE_S)
            rows = walk.run()
    except LoginRejected as e:
        return CheckResult.skipped(f"{e}: новых попыток входа нет")
    except (NoFreeSession, LoginFailed) as e:
        return CheckResult.stand_error(str(e))
    except http.TransportError as e:
        return CheckResult.stand_error(f"камера не ответила на вход: {type(e).__name__}")
    return CheckResult(
        Status.PASSED,
        actual=f"API „Рувер“: {len(api.operations)} операций",
        children=rows,
    )
