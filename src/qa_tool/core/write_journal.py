"""Журнал незавершённой записи: что менялось на камере и было ли возвращено.

Общий для изменяющих проверок — веб-обхода (этап 5), картинки (этап 7) и кодеков (этап 8); у каждой
свой вид журнала (`Journal`) и своя папка. Проверка пишет сюда до первого изменения настройки и
закрывает журнал, только сверив возврат. Файл нужен потому, что процесс камеры могут снять в любой
момент: тогда в протоколе всё равно должна быть строка «блокер» с настройкой и значениями (S14, S15
этапа 5; S8, S9 этапа 7), а следующая проверка этой камеры должна знать, что её настройка осталась
изменённой.

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

RUN_ID_KEY = "run_id"
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
    # как вернуть, если в page и old этого мало: картинке — токен источника и исходный XML
    restore: dict[str, str] = field(default_factory=dict)


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


def write_atomic(path: Path, text: str) -> None:
    """Записать текстовый файл атомарно и с `fsync`: на диске либо старый файл, либо новый.

    Args:
        path: файл; его папка создаётся при необходимости.
        text: содержимое, UTF-8.

    Raises:
        OSError: файл не записан; прежний файл, если был, не тронут.
    """
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    _sync_folder(folder)


def save(folder: Path, entry: PendingWrite) -> None:
    """Записать журнал атомарно и с `fsync`.

    Args:
        folder: папка журналов.
        entry: запись; адрес страницы очищается, время ставится текущее.

    Raises:
        OSError: журнал не записан. Менять поле нельзя: вернуть его будет нечем.
    """
    data = asdict(entry) | {
        "page": safe_page(entry.page),
        "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    text = json.dumps(data, ensure_ascii=False, indent=1)
    write_atomic(journal_file(folder, entry.address, entry.port), text)


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


def lost_write_result(entry: PendingWrite, where: str = "страница") -> CheckResult:
    """Строка протокола «блокер»: какая настройка, какое было значение, какое могло остаться.

    Args:
        entry: запись журнала.
        where: чем назвать `entry.page` в протоколе — `Journal.where` вида журнала.
    """
    stage = _STAGE_TITLES.get(entry.stage, entry.stage)
    reason = f"настройка «{entry.label}» могла остаться изменённой: {stage}"
    return CheckResult(
        Status.FAILED,
        reason=redact(reason),
        expected=f"значение возвращено: {redact(entry.old)}",
        actual=f"было {redact(entry.old)}, записывалось {redact(entry.new)}; {where} {entry.page}",
    )


@dataclass(frozen=True)
class Journal:
    """Вид журнала записи: папка рядом с прогонами, ключ папки в `CheckContext.extra`, строка
    «блокер» для снятого процесса и чем в протоколе назван адрес из `PendingWrite.page`."""

    folder_name: str
    folder_key: str
    lost_spec: CheckSpec
    where: str

    def folder(self, runs_dir: Path) -> Path:
        """Папка журналов этого вида."""
        return runs_dir / self.folder_name


def _lost_spec(check_id: str, module: str, title: str, how: str) -> CheckSpec:
    return CheckSpec(
        check_id,
        module,
        title,
        f"Программа меняла настройку {how} и не успела вернуть её: процесс проверки камеры был "
        "снят. Настройку нужно проверить на камере вручную.",
        lambda _ctx: CheckResult.skipped("строка журнала, отдельно не запускается"),
        severity=Severity.BLOCKER,
    )


WEB = Journal(
    "pending-web",
    "web_journal_dir",
    _lost_spec(
        "WEB-WRITE-LOST", "web", "Запись веб-интерфейса не завершена", "через веб-интерфейс"
    ),
    "страница",
)
IMAGE = Journal(
    "pending-image",
    "image_journal_dir",
    _lost_spec(
        "IMAGE-WRITE-LOST", "image", "Запись настроек картинки не завершена", "картинки по ONVIF"
    ),
    "служба",
)
CODEC = Journal(
    "pending-codec",
    "codec_journal_dir",
    _lost_spec(
        "CODEC-WRITE-LOST",
        "codec",
        "Запись настроек кодировщика не завершена",
        "кодировщика по ONVIF",
    ),
    "служба",
)
JOURNALS = (WEB, IMAGE, CODEC)  # планировщик ищет строки «блокер» в каждом
