"""API-RUVER-1: все операции API камеры «Рувер» по карте, строка протокола на операцию.

Одна составная проверка на камеру, а не проверка на раздел: сеанс и исходные значения живут в
одном контексте, и `finally` выполняется всегда. Планировщик снимает процесс только по
`timeout_s`, поэтому у обхода свой предел времени меньше него: при пределе и при остановке новые
значения не пишутся, а записанное возвращается и сверяется до выхода (AD-3).

Порядок: чтение всех настроек → запись с возвратом → сверка обходом чтения → экспорт настроек →
разрушающие, перезагрузка последней. Первое чтение одновременно служит снимком «до». Экспорт идёт
после сверки: на прошивке 0.6.14 он перезапускает службу API, связь пропадает на несколько секунд.

Операции функций, которых у модели нет по ответу `/api/hardware`, не вызываются: камера отвечает
на них ошибками или пустотой, и это не дефект прошивки.

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
    RETURN_ONLY,
    TABOO,
    WRITE,
    ApiMap,
    LoginFailed,
    LoginRejected,
    NoFreeSession,
    Operation,
    RuverClient,
    load_map,
)

CHECK_ID = "API-RUVER-1"
RESTORE_ID = "API-RUVER-RESTORE"
NEED_REBOOT = "/api/service/need_reboot"
INFO = "/api/info"
HARDWARE = "/api/hardware"
EXPORT = "/api/service/export_settings"
REBOOT = "/api/service/reboot"
BLOCKS = "/api/osd/block_positions"
SET_BLOCK = "/api/osd/set_block_position"
STREAMS = ("primary", "secondary", "preview")
NO_STREAM = object()  # такого потока у камеры нет, в отличие от сбоя чтения
DOWN_MISSES = 2  # одиночный тайм-аут — ещё не уход в перезагрузку
# Камера не ответила или повторный вход не удался. CallRefused сюда не входит: это ошибка программы.
LOST = (http.TransportError, LoginRejected, LoginFailed, NoFreeSession)
# Адрес → признак в ответе /api/hardware. Сверено на камере TMID-018 с прошивкой 0.6.14.
FEATURES = {
    "/api/lte": "lteSupport",
    "/api/glonass": "glonassSupport",
    "/api/osd/glonass_enable": "glonassSupport",
    "/api/network/https": "httpsSupport",
    "/api/sdcard": "flashCardSupport",
    "/api/recorder": "recorderSupport",
    "/api/shock_sensor": "shockSensorSupport",
    "/api/image/exposure/iris_enable": "irisSupport",
}
LEFT_CHANGED = "камера осталась изменённой"
NOT_DOWN = "камера не ушла в перезагрузку"
NOT_RETURNED = "значение не вернулось"
PER_OPERATION_S = 3.0  # обычно 0,1 с; запас на медленную камеру и тайм-ауты отдельных запросов
RESERVE_S = 300.0  # возврат значений и сверка уже после предела времени
REBOOT_WAIT_S = 180.0  # камера «Рувер» поднимается за 1–2 минуты
REBOOT_POLL_S = 3.0
REBOOT_DOWN_S = 60.0  # за это время камера должна уйти в перезагрузку; на камере не измерено
RETURN_PAUSE_S = 2.0
OUTAGE_WATCH_S = 6.0  # на 0.6.14 обрыв начинается в первую секунду после экспорта
OUTAGE_LIMIT_S = 30.0  # на 0.6.14 служба возвращается за 3 с
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
    """Имена разошедшихся полей двух ответов; значения намеренно не возвращаются.

    Порядок в списках не считается изменением: на TMID-018 0.6.14 камера сама меняет порядок
    блоков наложения. Элементы с `id` сравниваются по нему, чтобы было видно, какой разошёлся.
    """
    name = prefix.rstrip(".") or "ответ"
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after))
        return [
            found
            for key in keys
            for found in changed_fields(before.get(key), after.get(key), f"{prefix}{key}.")
        ]
    if isinstance(before, list) and isinstance(after, list):
        old, new = _by_id(before), _by_id(after)
        if old is not None and new is not None:
            return [
                found
                for key in sorted(set(old) | set(new), key=str)
                for found in changed_fields(old.get(key), new.get(key), f"{name}[{key}].")
            ]
        return [] if _canonical(before) == _canonical(after) else [name]
    return [] if before == after else [name]


def _by_id(items: list) -> dict | None:
    """Элементы списка по `id`; None — список не из таких элементов."""
    if not all(isinstance(item, dict) and isinstance(item.get("id"), int | str) for item in items):
        return None
    found = {item["id"]: item for item in items}
    return found if len(found) == len(items) else None  # одинаковые id скрыли бы разницу


def _canonical(items: list) -> list[str]:
    return sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items)


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
    features: dict[str, Any] = field(default_factory=dict)  # ответ /api/hardware
    blocks: dict[str, list] = field(default_factory=dict)  # надписи по потокам до записи
    blocks_unread: list[str] = field(default_factory=list)  # потоки без снимка: сверить нельзя

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

        Сведения о камере идут первыми: версия прошивки нужна в причинах 404, а состав
        функций — чтобы не вызывать то, чего у модели нет. Экспорт — отдельно (`export`).
        """
        reads = [op for op in self.api.operations if op.kind == READ and op.path != EXPORT]
        first = {INFO: 0, HARDWARE: 1}
        for op in sorted(reads, key=lambda op: first.get(op.path, 2)):
            self._row(op, self._read(op))
        if self.ctx.allow_writes:
            for stream in STREAMS:
                found = self._blocks_of(stream)
                if isinstance(found, list):
                    self.blocks[stream] = found
                elif found is not NO_STREAM:
                    self.blocks_unread.append(stream)

    def _absent(self, op: Operation) -> str:
        """Причина пропуска, если функции операции у этой модели нет; пусто — функция есть."""
        for prefix, flag in FEATURES.items():
            if op.path.startswith(prefix) and self.features.get(flag) is False:
                return f"нет у этой модели: {flag} = false в /api/hardware"
        return ""

    def _read(self, op: Operation) -> CheckResult:
        if op.missing:
            return CheckResult.skipped(f"нужны параметры: {', '.join(op.missing)}")
        absent = self._absent(op)
        if absent:
            return CheckResult.skipped(absent)
        halted = self._halted()
        if halted:
            return CheckResult.skipped(halted)
        try:
            response = self.client.call(op)
        except LOST as e:
            return CheckResult.stand_error(f"камера не ответила: {type(e).__name__}: {e}")
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
        # Снимок и состав функций нужны и при расхождении со схемой: она отстаёт от прошивки,
        # а адрес без снимка сверка не увидела бы и написала бы «совпало».
        self.before[op.id] = data
        if op.path == HARDWARE and isinstance(data, dict):
            self.features = data
        if op.path == INFO and isinstance(data, dict):
            self.firmware = str(data.get("firmwareVersion") or "")
        errors = schema_errors(op.schema, data, op.sensitive)
        if errors:
            return self._failed(
                self._clean("ответ не совпал со схемой: " + "; ".join(errors[:5]))[:500],
                expected,
                evidence,
            )
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
            halted = self._halted() or self._absent(op)
            if halted:
                self._row(op, CheckResult.skipped(halted))
                continue
            try:
                self._write(op)
            except LOST as e:
                reason = f"камера не ответила: {type(e).__name__}: {e}"
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
        result = self._change_and_return(op, read_op, original, value)
        self._row(op, result)
        outside = beyond_limits(op, limits)
        if outside is None or self._halted():
            return
        if LEFT_CHANGED in result.reason:
            # от неверной точки отсчёта строка показала бы ложное «значение изменилось»
            reason = f"{NOT_RETURNED}: вне пределов не проверялось"
            self._row(op, CheckResult.skipped(reason), "-LIMIT")
            return
        self._row(op, self._try_outside(op, read_op, original, outside), "-LIMIT")

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
        except (ValueError, *LOST) as e:
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
                # TMID-018 0.6.14: возврат режима день/ночь сразу после смены получил 400, а при
                # сверке в конце обхода тот же запрос прошёл. Пауза 2 с на камере не проверена.
                time.sleep(float(self.ctx.extra.get("api_return_pause_s", RETURN_PAUSE_S)))
                response = self.client.call(op, {op.setting: original})
            if not response.is_success:
                return f"возврат отказан: код {response.status_code}"
            current, _ = self._read_field(read_op, op.setting)
        except (ValueError, *LOST) as e:
            return f"возврат не подтверждён: {type(e).__name__}"
        return "" if current == original else f"{op.setting} = {current}, было {original}"

    def _try_outside(
        self, op: Operation, read_op: Operation, original: Any, value: int
    ) -> CheckResult:
        """Отправить значение вне пределов: камера обязана отказать и не менять настройку (S7).

        Значение возвращается и после сбоя: ответ мог потеряться, а запись примениться.
        """
        expected = f"{op.setting} = {value} вне пределов: ошибка, значение не меняется"
        self.wrote = True
        evidence: list[Evidence] = []
        try:
            response = self.client.call(op, {op.setting: value})
            evidence = [self._evidence(op, response, {op.setting: value})]
            current, _ = self._read_field(read_op, op.setting)
        except (ValueError, *LOST) as e:
            problem = self._return_value(op, read_op, original)
            if problem:
                return self._failed(f"{LEFT_CHANGED}: {problem}", expected, evidence)
            return CheckResult.stand_error(f"значение не прочитано: {type(e).__name__}: {e}")
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
            if op.kind != READ or op.live or op.binary or op.schema is None:
                continue
            if op.id not in self.before:
                # первое чтение не удалось: снимка нет, и «совпало» здесь было бы неправдой
                row = self.rows.get(op.id)
                if row and row.result.status in (Status.FAILED, Status.STAND_ERROR):
                    unread.append(op.path)
                continue
            if time.monotonic() > self.hard_deadline:
                unread.append(op.path)
                continue
            try:
                response = self.client.call(op)
                if response.status_code != 200:
                    unread.append(op.path)
                    continue
                data = response.json()  # ответ `null` — тоже ответ: так камера отдаёт пустой список
            except (ValueError, *LOST):
                unread.append(op.path)
                continue
            fields = changed_fields(self.before[op.id], data)
            if fields:
                changed[op.path] = fields
        return changed, unread

    def _repair(self, changed: dict[str, list[str]]) -> None:
        """Ещё раз вернуть настройки вида «запись», которые разошлись со снимком «до»."""
        for path in changed:
            if time.monotonic() > self.hard_deadline:
                return
            op, read_op = self.api.get("POST", path), self.api.get("GET", path)
            if op is None or op.kind != WRITE or read_op.id not in self.before:
                continue
            before = self.before[read_op.id]
            if isinstance(before, dict) and op.setting in before:
                self._return_value(op, read_op, before[op.setting])

    def _blocks_of(self, stream: str) -> Any:
        """Надписи потока; `NO_STREAM` — потока у камеры нет; None — чтение не удалось."""
        read_op = self.api.get("GET", BLOCKS)
        try:
            response = self.client.call(read_op, query={"stream_id": stream})
            if response.status_code in (400, 404):
                return NO_STREAM
            blocks = response.json()["overlayBlocks"] if response.status_code == 200 else None
        except (ValueError, KeyError, TypeError, *LOST):
            return None
        return blocks if isinstance(blocks, list) else None

    def _moved_blocks(self) -> tuple[list[tuple[str, dict]], list[str]]:
        """Надписи не на месте: (поток, положение до записи); и потоки, которые не прочитаны."""
        moved, unread = [], list(self.blocks_unread)
        for stream, before in self.blocks.items():
            found = self._blocks_of(stream)
            now = _by_id(found) if isinstance(found, list) else None
            if now is None:
                unread.append(stream)
                continue
            for block in before:
                current = now.get(block.get("id")) if isinstance(block, dict) else None
                has_place = current and "x" in block and "y" in block
                if has_place and (current.get("x"), current.get("y")) != (block["x"], block["y"]):
                    moved.append((stream, block))
        return moved, unread

    def _return_blocks(self, moved: list[tuple[str, dict]]) -> None:
        """Вернуть сдвинутые надписи по снимку «до»; сдвиг остаётся задачей программистам."""
        op = self.api.get("POST", SET_BLOCK)
        problems = []
        for stream, block in moved:
            body = {"id": block["id"], "x": block["x"], "y": block["y"]}
            try:
                response = self.client.call(op, body, {"stream_id": stream})
                if not response.is_success:
                    problems.append(f"{stream}: код {response.status_code}")
            except LOST as e:
                problems.append(f"{stream}: {type(e).__name__}")
        names = ", ".join(f"{stream} №{block['id']}" for stream, block in moved)
        reason = f"после записи настроек сдвинулись надписи: {names}"
        text = (
            f"{reason}\n{'; '.join(problems) or 'запрос возврата принят, итог — в строке сверки'}"
        )
        evidence = [Evidence(f"{op.id}.txt", self._clean(text))]
        self._row(op, self._failed(reason, "запись настроек не двигает надписи", evidence))

    def _settle_rows(self, changed: dict[str, list[str]], unread: list[str]) -> None:
        """Снять «осталась изменённой» со строк настроек, которые вернулись при сверке."""
        for op in self.api.operations:
            read_op = self.api.get("GET", op.path)
            if op.kind != WRITE or op.path in changed or op.path in unread:
                continue
            if read_op.id not in self.before:
                continue
            for row_id in (op.id, f"{op.id}-LIMIT"):
                row = self.rows.get(row_id)
                if row and LEFT_CHANGED in row.result.reason:
                    text = row.result.reason.replace(LEFT_CHANGED, "вернулось только при сверке")
                    row.result.reason = row.result.actual = text
                elif row and NOT_RETURNED in row.result.reason:
                    row.result.reason = row.result.reason.replace(
                        NOT_RETURNED, "значение вернулось только при сверке"
                    )

    def verify(self) -> None:
        """Строка сверки: настройки после записи совпали со снимком «до» (S6)."""
        changed, unread = self._read_again()
        first = list(changed)
        self._repair(changed)
        moved, _ = self._moved_blocks()
        if moved:
            self._return_blocks(moved)
        if changed or moved:
            changed, unread = self._read_again()
        repaired = [path for path in first if path not in changed and path not in unread]
        self._settle_rows(changed, unread)
        lines = [f"{path}: {', '.join(fields)}" for path, fields in changed.items()]
        left, streams = self._moved_blocks()
        lines += [f"{BLOCKS}?stream_id={stream}: надпись №{b['id']}" for stream, b in left]
        unread = [*unread, *(f"{BLOCKS}?stream_id={stream}" for stream in streams)]
        expected = "настройки после записи совпадают с обходом чтения до неё"
        text = "\n".join(
            ["Разошлись:", *lines, "Не прочитаны:", *unread, "Вернулись при сверке:", *repaired]
        )
        evidence = [Evidence(f"{RESTORE_ID}.txt", self._clean(text))]
        if lines:
            result = self._failed(f"{LEFT_CHANGED}: " + "; ".join(lines)[:400], expected, evidence)
        elif unread:
            result = CheckResult.stand_error(f"сверка не закончена: {len(unread)} адресов")
            result.evidence = evidence
        else:
            actual = f"совпало после повторного возврата: {', '.join(repaired)}" if repaired else ""
            result = CheckResult(
                Status.PASSED, expected=expected, actual=actual or "совпало", evidence=evidence
            )
        self.rows[RESTORE_ID] = ChildResult(
            RESTORE_ID, "Сверка настроек после записи через API", result
        )

    # --- разрушающие ------------------------------------------------------------------------

    def export(self) -> None:
        """Экспорт настроек и замер обрыва службы API после него."""
        op = self.api.get("POST", EXPORT)
        result = self._read(op)
        if result.status is Status.PASSED:
            outage = self._outage_s()
            if outage:
                result = self._failed(
                    f"после экспорта API не отвечало {outage:.0f} с: служба перезапустилась",
                    "экспорт не прерывает работу API",
                    result.evidence,
                )
        self._row(op, result)

    def _outage_s(self) -> float:
        """Сколько секунд API не отвечало после экспорта; 0 — обрыва не было."""
        self.client.expect_restart()
        info = self.api.get("GET", INFO)
        poll = float(self.ctx.extra.get("api_outage_poll_s", 1.0))
        start = time.monotonic()
        lost = None
        limit = start + float(self.ctx.extra.get("api_outage_watch_s", OUTAGE_WATCH_S))
        while time.monotonic() < limit:
            try:
                answered = self.client.call(info).status_code == 200
            except LOST:
                answered = False
            if answered and lost is not None:
                return max(time.monotonic() - lost, 0.1)
            if not answered and lost is None:
                lost = time.monotonic()
                limit = lost + OUTAGE_LIMIT_S
            time.sleep(poll)
        return time.monotonic() - lost if lost is not None else 0.0

    def destroy_all(self) -> None:
        """Разрушающие операции; перезагрузка — последней, после неё сеанс новый."""
        operations = [op for op in self.api.operations if op.kind == DESTRUCTIVE]
        for op in sorted(operations, key=lambda op: op.path == REBOOT):
            if not self.ctx.allow_destructive:
                self._row(op, CheckResult.skipped("не разрешено сценарием: разрушающая проверка"))
            elif op.missing:
                self._row(op, CheckResult.skipped(f"нужны параметры: {', '.join(op.missing)}"))
            elif self._absent(op):
                self._row(op, CheckResult.skipped(self._absent(op)))
            elif self._halted():
                self._row(op, CheckResult.skipped(self._halted()))
            else:
                self._row(op, self._destroy(op))

    def _destroy(self, op: Operation) -> CheckResult:
        expected = "камера выполнила операцию"
        try:
            response = self.client.call(op)
        except http.NOT_SENT as e:
            return CheckResult.stand_error(f"запрос не отправлен: {type(e).__name__}")
        except LOST as e:
            if op.path != REBOOT or not isinstance(e, http.TransportError):
                return CheckResult.stand_error(f"камера не ответила: {type(e).__name__}: {e}")
            # камера может уйти в перезагрузку, не успев ответить: судить надо по её уходу
            note = f"{op.method} {op.path}\n\nответа нет: {type(e).__name__}"
            evidence = [Evidence(f"{op.id}.txt", self._clean(note))]
            problem = self._came_back()
            if problem == NOT_DOWN:
                result = CheckResult.stand_error("ответа нет, и камера не ушла в перезагрузку")
                result.evidence = evidence
                return result
            if problem:
                return self._failed(problem, expected, evidence)
            return CheckResult(
                Status.PASSED, expected=expected, actual="ушла и вернулась", evidence=evidence
            )
        refused = self._refused(op, response)
        if refused:
            return refused
        evidence = [self._evidence(op, response)]
        if not response.is_success:
            return self._failed(f"код {response.status_code}", expected, evidence)
        problem = self._came_back() if op.path == REBOOT else ""
        if problem:
            return self._failed(problem, expected, evidence)
        return CheckResult(Status.PASSED, expected=expected, actual="выполнено", evidence=evidence)

    def _came_back(self) -> str:
        """Дождаться, что камера ушла в перезагрузку и вернулась; пусто — так и было.

        Первый ответ сам по себе ни о чём не говорит: служба работает и до ухода камеры,
        поэтому сначала нужен обрыв связи.
        """
        self.client.expect_restart()
        info = self.api.get("GET", INFO)
        extra = self.ctx.extra
        poll = float(extra.get("api_reboot_poll_s", REBOOT_POLL_S))
        limit = time.monotonic() + float(extra.get("api_reboot_down_s", REBOOT_DOWN_S))
        misses = 0
        while misses < DOWN_MISSES:
            misses = 0 if self._reachable(info) else misses + 1
            if misses < DOWN_MISSES and time.monotonic() > limit:
                return NOT_DOWN
            time.sleep(poll)
        wait_s = float(extra.get("api_reboot_wait_s", REBOOT_WAIT_S))
        limit = time.monotonic() + wait_s
        while time.monotonic() < limit:
            if self._answers(info):
                return ""
            time.sleep(poll)
        return f"камера не вернулась за {wait_s:g} с"

    def _reachable(self, info: Operation) -> bool:
        """Есть ли связь со службой API: любой ответ, даже отказ при входе, значит, что есть."""
        try:
            self.client.call(info)
        except http.TransportError:
            return False
        except LOST:
            return True
        return True

    def _answers(self, info: Operation) -> bool:
        """Вернулась ли служба API: чтение отвечает 200."""
        try:
            return self.client.call(info).status_code == 200
        except LOST:
            return False

    # --- итог -------------------------------------------------------------------------------

    def run(self) -> list[ChildResult]:
        """Весь обход; строки в порядке карты, сверка — последней."""
        self.read_all()
        self.write_all()
        if self.wrote:
            self.verify()
        self.export()
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
            elif op.kind == RETURN_ONLY:
                self._row(op, CheckResult.skipped(op.reason))
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
