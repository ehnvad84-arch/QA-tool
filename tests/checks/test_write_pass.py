"""Общий проход изменяющей проверки (этап 8, T3): журнал прошлого прогона, пропавшая камера,
строка возврата. Видео и камера не нужны: значения и возврат подставляются функциями."""

from dataclasses import dataclass, field

import pytest

from qa_tool.checks import write_pass
from qa_tool.checks.write_pass import LOST, WritePass, lost_abort, restore_row, total
from qa_tool.core.checks import CheckContext, CheckResult, ChildResult, Status
from qa_tool.core.models import Camera, Place
from qa_tool.core.write_journal import (
    CODEC,
    IMAGE,
    JOURNALS,
    RUN_ID_KEY,
    UNREADABLE,
    PendingWrite,
    load,
    save,
)


class Refused(RuntimeError):
    """Отказ драйвера отправить возврат."""


def context(tmp_path, journal=CODEC, allow_writes=True, run_id="QA-NEW") -> CheckContext:
    camera = Camera(slot=2, address="10.0.0.5", username="admin", port=8080)
    extra = {journal.folder_key: str(tmp_path), RUN_ID_KEY: run_id}
    return CheckContext(camera, "secret", Place.COLOR_BOX, extra=extra, allow_writes=allow_writes)


def entry(**fields) -> PendingWrite:
    base = dict(
        run_id="QA-OLD",
        slot=2,
        address="10.0.0.5",
        port=8080,
        vendor="generic",
        page="http://cam/onvif/media2",
        label="Кодировщик 1",
        kind="enc-1",
        old="H264 4096 кбит/с пост.",
        new="H265 1024 кбит/с пост.",
        stage="sent",
        restore={"token": "enc-1", "xml": "<Configuration/>"},
    )
    return PendingWrite(**(base | fields))


def settle(ctx, now: str, back: str | None = None, error: Exception | None = None):
    calls: list[str] = []

    def put_back(e: PendingWrite) -> str:
        calls.append(e.restore["xml"])
        if error is not None:
            raise error
        return e.old if back is None else back

    passer = WritePass(ctx, CODEC)
    passer.settle_old(lambda _e: now, put_back, (Refused,))
    return passer, calls


def test_codec_journal_is_registered_with_its_own_folder():
    assert CODEC in JOURNALS
    assert CODEC.folder_name == "pending-codec" and CODEC.folder_key == "codec_journal_dir"
    assert CODEC.lost_spec.id == "CODEC-WRITE-LOST"
    assert len({j.folder_name for j in JOURNALS}) == len(JOURNALS)


def test_our_value_left_by_previous_run_is_returned(tmp_path):
    save(tmp_path, entry())
    passer, calls = settle(context(tmp_path), now="H265 1024 кбит/с пост.")
    [row] = passer.rows
    assert calls == ["<Configuration/>"]
    assert row.check_id == "CODEC-WRITE-LOST" and row.result.status is Status.PASSED
    assert "возвращена" in row.result.reason
    assert load(tmp_path, "10.0.0.5", 8080) is None


def test_value_already_original_closes_journal_without_writing(tmp_path):
    save(tmp_path, entry())
    passer, calls = settle(context(tmp_path), now="H264 4096 кбит/с пост.")
    assert calls == []
    assert passer.rows[0].result.status is Status.PASSED and "уже исходное" in (
        passer.rows[0].result.reason
    )
    assert load(tmp_path, "10.0.0.5", 8080) is None


@pytest.mark.parametrize(
    ("kwargs", "stored", "words"),
    [
        ({"allow_writes": False}, {}, "вернуть нельзя"),
        ({}, {"stage": UNREADABLE}, "журнал повреждён"),
        ({}, {"restore": {"token": "enc-1"}}, "вернуть нечем"),
    ],
)
def test_previous_write_that_cannot_be_returned_is_a_blocker(tmp_path, kwargs, stored, words):
    save(tmp_path, entry(**stored))
    passer, calls = settle(context(tmp_path, **kwargs), now="H265 1024 кбит/с пост.")
    [row] = passer.rows
    assert calls == []
    assert row.result.status is Status.FAILED and row.severity.value == "блокер"
    assert words in row.result.reason and "журнал прошлого прогона" in row.result.reason
    # блокер в протоколе — следующий прогон его не повторяет
    assert load(tmp_path, "10.0.0.5", 8080) is None


def test_value_changed_by_someone_else_is_not_overwritten(tmp_path):
    save(tmp_path, entry())
    passer, calls = settle(context(tmp_path), now="H264 2048 кбит/с пер.")
    assert calls == []
    assert "поменял кто-то другой" in passer.rows[0].result.reason


@pytest.mark.parametrize("error", [Refused("нет службы"), ValueError("не XML")])
def test_refused_return_is_a_blocker(tmp_path, error):
    save(tmp_path, entry())
    passer, _ = settle(context(tmp_path), now="H265 1024 кбит/с пост.", error=error)
    assert "возврат не отправлен" in passer.rows[0].result.reason


def test_unconfirmed_return_is_a_blocker(tmp_path):
    save(tmp_path, entry())
    passer, _ = settle(context(tmp_path), now="H265 1024 кбит/с пост.", back="H265 1024")
    assert "не подтвердился" in passer.rows[0].result.reason


def test_journal_of_this_run_or_other_kind_is_left_alone(tmp_path):
    save(tmp_path, entry(run_id="QA-NEW"))
    passer, calls = settle(context(tmp_path), now="H265 1024 кбит/с пост.")
    assert passer.rows == [] and calls == []
    assert load(tmp_path, "10.0.0.5", 8080) is not None

    # папка «Картинки» проходу «Кодеков» не видна
    image_dir = tmp_path / "image"
    save(image_dir, entry())
    image_ctx = context(image_dir, journal=IMAGE)
    passer = WritePass(image_ctx, CODEC)
    passer.settle_old(lambda _e: "x", lambda _e: "x")
    assert passer.rows == [] and load(image_dir, "10.0.0.5", 8080) is not None


def test_note_and_close_follow_the_stages(tmp_path):
    passer = WritePass(context(tmp_path), CODEC)
    passer.note("http://cam/onvif/media2", "Кодировщик 1", "enc-1", "a", "b", "intent", {"x": "1"})
    saved = load(tmp_path, "10.0.0.5", 8080)
    assert saved is not None and saved.stage == "intent" and saved.run_id == "QA-NEW"
    assert (saved.slot, saved.new, saved.restore) == (2, "b", {"x": "1"})
    passer.close()
    assert load(tmp_path, "10.0.0.5", 8080) is None


def test_without_journal_folder_nothing_is_written(tmp_path):
    camera = Camera(slot=1, address="10.0.0.5", username="admin")
    passer = WritePass(CheckContext(camera, "", Place.COLOR_BOX, allow_writes=True), CODEC)
    passer.note("u", "l", "k", "a", "b", "intent", {})
    passer.settle_old(lambda _e: "", lambda _e: "")
    passer.close()
    assert passer.rows == [] and list(tmp_path.iterdir()) == []


def test_camera_back_waits_only_when_lost(tmp_path, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(write_pass, "camera_alive", lambda _cam: next(answers))
    monkeypatch.setattr(write_pass, "POLL_S", 0.01)
    passer = WritePass(context(tmp_path), CODEC)
    assert passer.camera_back(5.0) == ""  # камера не пропадала — не спрашиваем
    passer.lost = True
    assert passer.camera_back(5.0) == ""


def test_camera_that_does_not_return_keeps_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(write_pass, "camera_alive", lambda _cam: False)
    monkeypatch.setattr(write_pass, "POLL_S", 0.01)
    passer = WritePass(context(tmp_path), CODEC)
    passer.lost = True
    assert "не вернулась за 0.05 с" in passer.camera_back(0.05)


def test_lost_abort_marks_current_row_failed():
    abort = lost_abort(OSError("нет ответа"), "битрейт держится")
    assert abort.current.status is Status.FAILED and abort.current.reason == LOST
    assert abort.current.expected == "битрейт держится" and LOST in abort.rest


@dataclass
class Report:
    left: list[tuple[str, str, str]] = field(default_factory=list)
    other: list[str] = field(default_factory=list)


@pytest.mark.parametrize(
    ("touched", "report", "problem", "status", "words"),
    [
        ([], None, "", Status.SKIPPED, "ничего не менялось"),
        (["enc-1"], None, "камера не вернулась", Status.FAILED, "камера не вернулась"),
        (["enc-1"], Report(left=[("кодек", "H264", "H265")]), "", Status.FAILED, "не вернулись"),
        (["enc-1"], Report(), "", Status.PASSED, "возвращены и сверены: enc-1"),
    ],
)
def test_restore_row(touched, report, problem, status, words):
    row = restore_row(
        "CODEC-RESTORE-1", "Кодеки: возврат", touched, report, problem, "ничего не менялось"
    )
    assert row.check_id == "CODEC-RESTORE-1" and row.severity.value == "блокер"
    assert row.result.status is status and words in row.result.reason


def test_total_fails_on_any_failed_row():
    ok = ChildResult("A-1", "a", CheckResult(Status.PASSED))
    bad = ChildResult("A-2", "b", CheckResult(Status.FAILED, reason="нет"))
    assert total([ok]).status is Status.PASSED
    assert total([ok, bad]).status is Status.FAILED and total([ok, bad]).children == [ok, bad]
