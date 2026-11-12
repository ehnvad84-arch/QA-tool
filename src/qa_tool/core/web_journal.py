"""Журнал незавершённой записи веб-обхода: что менялось на камере и было ли возвращено.

Обход пишет сюда до первого изменения поля и закрывает журнал, только сверив возврат. Файл нужен
потому, что процесс камеры могут снять в любой момент: тогда в протоколе всё равно должна быть
строка «блокер» с настройкой и значениями (S14, S15), а следующий обход этой камеры должен знать,
что её настройка осталась изменённой.

Файл лежит рядом с папками прогонов и переживает их: один файл на адрес и порт. Запись атомарная:
сбой питания оставляет либо старый журнал, либо новый, но не обрывок. Значения — ответ камеры,
то есть недоверенные данные; адрес страницы хранится без запроса и логина.
"""

import contextlib
import datetime
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from qa_tool.core.checks import CheckResult, CheckSpec, Severity, Status
from qa_tool.core.secrets import redact

FOLDER_KEY = "web_journal_dir"
RUN_ID_KEY = "run_id"
FOLDER_NAME = "pending-web"
LOST_WRITE_ID = "WEB-WRITE-LOST"
STAGES = ("intent", "sent", "verified", "restoring")
UNREADABLE = "не прочитан"
_STAGE_TITLES = {
    "intent": "запись готовилась, значение могло не измениться",
    "sent": "новое значение отправлено",
    "verified": "новое значение подтверждено, возврат не начат",
    "restoring": "возврат начат и не подтверждён",
    UNREADABLE: "журнал повреждён",
}


@dataclass
class PendingWrite:
    """Незавершённая запись одного поля. `stage` — из `STAGES`, по порядку выполнения."""

    run_id: str
    slot: int
    address: str
    port: int
    vendor: str
    page: str
    label: str
    kind: str
    old: str
    new: str
    stage: str = "intent"
    path: list[str] = field(default_factory=list)  # вкладки и пункты меню до поля
    fingerprint: str = ""
    time: str = ""
    reported: bool = False  # строка «блокер» уже в протоколе этого прогона


def journal_file(folder: Path, address: str, port: int) -> Path:
    """Файл журнала камеры. Адрес приходит из стенда, но в имя файла идёт только очищенным."""
    return folder / f"{re.sub(r'[^0-9A-Za-z.-]', '_', address)}_{port}.json"


def safe_page(url: str) -> str:
    """Адрес страницы без логина и запроса: в них бывают токены. Фрагмент — это страница SPA."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", parts.fragment))


def _sync_folder(folder: Path) -> None:
    """Закрепить на диске переименование файла. На Windows папку так открыть нельзя."""
    with contextlib.suppress(OSError):
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save(folder: Path, entry: PendingWrite) -> None:
    """Записать журнал атомарно и с `fsync`.

    Args:
        folder: папка журналов.
        entry: запись; адрес страницы очищается, время ставится текущее.

    Raises:
        OSError: журнал не записан. Менять поле нельзя: вернуть его будет нечем.
    """
    folder.mkdir(parents=True, exist_ok=True)
    data = asdict(entry) | {
        "page": safe_page(entry.page),
        "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    descriptor, name = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=1)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, journal_file(folder, entry.address, entry.port))
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    _sync_folder(folder)


def load(folder: Path, address: str, port: int) -> PendingWrite | None:
    """Прочитать журнал камеры.

    Returns:
        Запись; None — журнала нет. Повреждённый файл не считается отсутствием: камера могла
        остаться изменённой, поэтому возвращается запись со стадией `UNREADABLE`.
    """
    path = journal_file(folder, address, port)
    try:
        return PendingWrite(**json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (ValueError, TypeError):
        return PendingWrite(
            "", 0, address, port, "", "", "неизвестная настройка", "", "?", "?", UNREADABLE
        )


def clear(folder: Path, address: str, port: int) -> None:
    """Закрыть журнал: значение возвращено и сверено."""
    journal_file(folder, address, port).unlink(missing_ok=True)
    _sync_folder(folder)


def lost_write_result(entry: PendingWrite) -> CheckResult:
    """Строка протокола «блокер»: какая настройка, какое было значение, какое могло остаться."""
    stage = _STAGE_TITLES.get(entry.stage, entry.stage)
    reason = f"настройка «{entry.label}» могла остаться изменённой: {stage}"
    return CheckResult(
        Status.FAILED,
        reason=redact(reason),
        expected=f"значение возвращено: {redact(entry.old)}",
        actual=f"было {redact(entry.old)}, записывалось {redact(entry.new)}; страница {entry.page}",
    )


LOST_WRITE_SPEC = CheckSpec(
    LOST_WRITE_ID,
    "web",
    "Запись веб-интерфейса не завершена",
    "Программа меняла настройку через веб-интерфейс и не успела вернуть её: процесс проверки "
    "камеры был снят. Настройку нужно проверить на камере вручную.",
    lambda _ctx: CheckResult.skipped("строка журнала, отдельно не запускается"),
    severity=Severity.BLOCKER,
)
