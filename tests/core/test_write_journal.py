"""Журнал записи: атомарность, чтение, повреждение, очистка адреса."""

import json
import os

import pytest

from qa_tool.core.checks import Status
from qa_tool.core.write_journal import (
    UNREADABLE,
    PendingWrite,
    clear,
    journal_file,
    load,
    lost_write_result,
    safe_page,
    save,
)


def entry(**fields):
    base = dict(
        run_id="QA-1",
        slot=1,
        address="10.0.0.5",
        port=80,
        vendor="generic",
        page="http://10.0.0.5/image",
        label="Яркость",
        kind="range",
        old="50",
        new="60",
    )
    return PendingWrite(**(base | fields))


def test_saved_entry_is_read_back(tmp_path):
    save(tmp_path, entry(path=["Видео", "Изображение"], stage="sent"))
    read = load(tmp_path, "10.0.0.5", 80)
    assert (read.old, read.new, read.stage) == ("50", "60", "sent")
    assert read.path == ["Видео", "Изображение"] and read.time


def test_no_journal_is_none_and_clear_removes_it(tmp_path):
    assert load(tmp_path, "10.0.0.5", 80) is None
    save(tmp_path, entry())
    clear(tmp_path, "10.0.0.5", 80)
    assert load(tmp_path, "10.0.0.5", 80) is None
    clear(tmp_path, "10.0.0.5", 80)  # повторное закрытие не ошибка


def test_damaged_journal_is_not_treated_as_absent(tmp_path):
    save(tmp_path, entry())
    journal_file(tmp_path, "10.0.0.5", 80).write_text("{обрывок", encoding="utf-8")
    read = load(tmp_path, "10.0.0.5", 80)
    assert read.stage == UNREADABLE
    assert lost_write_result(read).status is Status.FAILED


def test_failed_write_leaves_old_journal_and_no_temp_file(tmp_path, monkeypatch):
    save(tmp_path, entry(stage="intent"))

    def refuse(*_):
        raise OSError("нет места")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        save(tmp_path, entry(stage="sent"))
    assert load(tmp_path, "10.0.0.5", 80).stage == "intent"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://admin:x@cam/a?token=1#/video", "http://cam/a#/video"),
        ("http://cam:8080/a", "http://cam:8080/a"),
    ],
)
def test_page_keeps_no_secrets(url, expected):
    assert safe_page(url) == expected


def test_address_cannot_leave_the_folder(tmp_path):
    assert journal_file(tmp_path, "../../etc/passwd", 80).parent == tmp_path


def test_blocker_row_names_values(tmp_path):
    result = lost_write_result(entry(stage="restoring"))
    assert result.status is Status.FAILED
    assert "Яркость" in result.reason and "возврат" in result.reason
    assert "было 50, записывалось 60" in result.actual


def test_old_journal_without_restore_field_still_loads(tmp_path):
    # журналы версии 0.5.0 писались без поля restore: камера с ними не должна стать «чистой»
    save(tmp_path, entry())
    path = journal_file(tmp_path, "10.0.0.5", 80)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["restore"]
    path.write_text(json.dumps(data), encoding="utf-8")
    read = load(tmp_path, "10.0.0.5", 80)
    assert read.stage == "intent" and read.restore == {}


def test_blocker_names_the_place_by_journal_kind():
    from qa_tool.core.write_journal import IMAGE

    result = lost_write_result(entry(page="http://cam/onvif/imaging"), IMAGE.where)
    assert "служба http://cam/onvif/imaging" in result.actual
