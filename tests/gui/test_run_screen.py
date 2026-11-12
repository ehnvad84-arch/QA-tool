import pytest
from PySide6.QtCore import Qt

from qa_tool.core.models import Camera
from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow, format_elapsed, stop_question
from qa_tool.gui.screens.run import RunScreen, safe_tip
from qa_tool.gui.state import AppState
from tests.fake_checks import SLOW
from tests.gui.test_scenario_run import fleet, make_window, screen  # noqa: F401 — фикстура

FAKE = "tests.fake_checks:REGISTRY"
CARD_CHECKS = ["FAKE-PASSPORT-1", "FAKE-COMPOSITE-1", "FAKE-FAILS-1"]


def finish(qtbot, window, timeout=60000):
    qtbot.waitUntil(lambda: not window.state.running, timeout=timeout)


def test_format_elapsed():
    assert format_elapsed(0) == "00:00:00"
    assert format_elapsed(14 * 60 + 32) == "00:14:32"
    assert format_elapsed(25 * 3600 + 61.9) == "25:01:01"


def test_s7_start_and_stop_from_any_screen(qtbot, tmp_path, fleet):  # noqa: F811
    window = make_window(qtbot, tmp_path, fleet[:2], registry_ref=FAKE, checks=SLOW)
    window.menu.setCurrentRow(1)  # «Камеры», не «Прогон»
    block = window.run_block
    assert block.start_button.isVisibleTo(window) and not block.stop_button.isVisibleTo(window)
    trace = tmp_path / "trace"
    trace.mkdir()
    block.start_button.click()
    window.run_control.runner.extra["trace_dir"] = str(trace)  # нужно фиктивным SLOW-проверкам
    assert window.state.running
    assert block.stop_button.isVisibleTo(window) and not block.start_button.isVisibleTo(window)
    assert block.status.text().startswith("Прогон идёт\n00:00:0")
    run = screen(window, RunScreen)
    qtbot.waitUntil(lambda: any(s.counts["прошла"] for s in run.view.slots.values()), timeout=30000)
    window.run_control.stop()
    assert block.status.text().startswith("Останавливаем")
    finish(qtbot, window)
    assert "остановлен" in block.status.text()
    assert block.protocols_button.isVisibleTo(window)
    block.protocols_button.click()
    assert window.current_screen().title == "Протоколы"


def test_s9_empty_stand(qtbot, tmp_path):
    create_app()
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    qtbot.addWidget(window)
    window.show()
    assert not window.run_block.start_button.isEnabled()
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    assert run.cameras.empty.isVisibleTo(window) and not run.cameras.list.isVisibleTo(window)
    assert not run.card.isVisibleTo(window)
    run.cameras.empty_link.click()
    assert window.current_screen().title == "Камеры"


def test_s1_s5_eight_rows_and_wrong_password_is_stand_error(
    qtbot,
    tmp_path,
    fleet,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("QA_TOOL_PASSWORD_6", "wrong")
    window = make_window(qtbot, tmp_path, fleet, checks=["CORE-CONNECT-1"])
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    assert run.cameras.list.count() == 8 and run.cameras.count.text() == "8 из 8 на стенде"
    window.run_block.start_button.click()
    finish(qtbot, window)
    rows = run.cameras.rows
    assert rows[6].dot.property("tone") == "stand"
    assert rows[6].summary.text().startswith("ошибка стенда:")
    assert run.view.slots[6].tasks == []
    assert rows[1].dot.property("tone") == "ok" and rows[1].summary.text() == "ошибок нет"
    assert rows[1].percent.text() == "100 %"
    # производитель — из паспорта камеры, а не адрес
    assert rows[1].maker.text() != "127.0.0.1"


def test_s2_s3_s4_s8_s11_card_of_selected_camera(qtbot, tmp_path, fleet):  # noqa: F811
    window = make_window(qtbot, tmp_path, fleet[:2], registry_ref=FAKE, checks=CARD_CHECKS)
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    opened = []
    run.opener = opened.append
    window.run_block.start_button.click()
    assert not run.card.tasks.protocol_link.isEnabled()  # протокола ещё нет
    finish(qtbot, window)
    run.cameras.list.setCurrentRow(1)  # место 2
    qtbot.waitUntil(lambda: run.card.place.text() == "Место 2", timeout=2000)
    card = run.card
    assert card.model.text() == "Фейк <b>FK-1</b>"
    assert card.model.textFormat() == Qt.TextFormat.PlainText  # разметка камеры — текстом
    assert card.details.text() == "Прошивка 1.0 · 127.0.0.1"
    assert card.chips["не прошла"].text() == "не прошла 2"
    assert card.chips["ошибка стенда"].isVisibleTo(window)  # SUB-3: «прошла» без доказательства
    labels = [title.text() for title, _ in card.steps.labels]
    states = [state.text() for _, state in card.steps.labels]
    assert labels == ["паспорт", "составная: 3 теста", "падает"]
    assert states == ["прошла", "не прошла", "не прошла"]
    assert card.tasks.titles == ["SUB-2", "FAKE-FAILS-1"]  # блокер раньше серьёзной
    assert card.tasks.protocol_link.isEnabled()
    card.tasks.protocol_link.click()
    assert [p.name for p in opened] == ["protocol_2.html"]


def test_keyboard_arrows_switch_card(qtbot, tmp_path, fleet):  # noqa: F811
    window = make_window(qtbot, tmp_path, fleet[:3], registry_ref=FAKE, checks=["FAKE-NET-1"])
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    run.cameras.list.setFocus()
    assert run.card.place.text() == "Место 1"
    qtbot.keyClick(run.cameras.list, Qt.Key.Key_Down)
    assert run.card.place.text() == "Место 2"


def running_window(qtbot, tmp_path, fleet):  # noqa: F811
    """Окно с идущим прогоном: первая проверка уже прошла, вторая ещё идёт."""
    window = make_window(qtbot, tmp_path, fleet, registry_ref=FAKE, checks=SLOW)
    asked = []
    window.ask_stop = lambda _parent, closing: asked.append(closing) or window.answer
    trace = tmp_path / "trace"
    trace.mkdir()
    window.run_block.start_button.click()
    window.run_control.runner.extra["trace_dir"] = str(trace)
    run = screen(window, RunScreen)
    qtbot.waitUntil(lambda: any(s.counts["прошла"] for s in run.view.slots.values()), timeout=30000)
    return window, asked


def test_s13_close_declined_keeps_run_going(qtbot, tmp_path, fleet):  # noqa: F811
    window, asked = running_window(qtbot, tmp_path, fleet[:1])
    window.answer = False
    assert window.close() is False
    assert asked == [True] and window.isVisible()
    assert window.state.running and not window.run_control.stop_requested
    window.run_control.stop()
    finish(qtbot, window)


def test_s13_close_confirmed_waits_for_protocols(qtbot, tmp_path, fleet):  # noqa: F811
    window, asked = running_window(qtbot, tmp_path, fleet[:1])
    window.answer = True
    assert window.close() is False  # окно ждёт протоколы
    assert window.isVisible() and window.run_control.stop_requested
    assert window.close() is False and asked == [True]  # второй крестик ничего не спрашивает
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=30000)
    assert (window.run_control.last_store.run_dir / "summary.html").exists()


def test_s13_stop_button_asks_first(qtbot, tmp_path, fleet):  # noqa: F811
    window, asked = running_window(qtbot, tmp_path, fleet[:1])
    window.answer = False
    window.run_block.stop_button.click()
    assert asked == [False] and not window.run_control.stop_requested
    window.answer = True
    window.run_block.stop_button.click()
    assert window.run_control.stop_requested
    finish(qtbot, window)
    assert window.isVisible()  # «Остановить» окно не закрывает


def test_s13_no_question_without_run(qtbot, tmp_path, fleet):  # noqa: F811
    window = make_window(qtbot, tmp_path, fleet[:1], registry_ref=FAKE, checks=SLOW)
    window.ask_stop = lambda *_: pytest.fail("без прогона вопроса быть не должно")
    assert window.close() is True


def test_s13_question_defaults_to_keep_running(qtbot):
    create_app()
    box = stop_question(None, closing=True)
    assert box.defaultButton().text() == "Продолжить прогон"
    assert box.escapeButton() is box.defaultButton()
    assert "Остановить и закрыть" in [b.text() for b in box.buttons()]


def run_card(qtbot, tmp_path, fleet):  # noqa: F811
    window = make_window(qtbot, tmp_path, fleet, registry_ref=FAKE, checks=CARD_CHECKS)
    window.run_block.start_button.click()
    finish(qtbot, window)
    return window, screen(window, RunScreen)


def test_second_run_starts_from_clean_screen(qtbot, tmp_path, fleet):  # noqa: F811
    window, run = run_card(qtbot, tmp_path, fleet[:1])
    first = dict(run.view.slots[1].counts)
    window.run_block.start_button.click()
    assert run.view.slots[1].percent == 0 and not run.view.slots[1].steps
    finish(qtbot, window)
    assert run.view.slots[1].counts == first
    assert len(run.view.slots[1].tasks) == 2


def test_run_started_from_other_screen_shows_cameras_added_there(qtbot, tmp_path, fleet):  # noqa: F811
    create_app()
    window = make_window(qtbot, tmp_path, [], registry_ref=FAKE, checks=["FAKE-NET-1"])
    run = screen(window, RunScreen)
    assert not run.cameras.rows
    window.menu.setCurrentRow(1)
    m = fleet[0]
    window.state.stand.add_camera(
        Camera(slot=1, address="127.0.0.1", port=m.port, rtsp_port=m.rtsp_port, username="admin")
    )
    trace = tmp_path / "trace"
    trace.mkdir()
    window.run_block.update_view()
    window.run_block.start_button.click()
    window.run_control.runner.extra["trace_dir"] = str(trace)
    assert list(run.cameras.rows) == [1]
    finish(qtbot, window)
    window.menu.setCurrentRow(3)
    assert list(run.cameras.rows) == [1] and run.view.slots[1].done


def test_result_survives_switching_screens(qtbot, tmp_path, fleet):  # noqa: F811
    window, run = run_card(qtbot, tmp_path, fleet[:1])
    window.menu.setCurrentRow(0)
    window.menu.setCurrentRow(3)
    assert run.view.slots[1].tasks and run.card.tasks.titles
    assert run.card.tasks.protocol_link.isEnabled()


def test_changed_stand_clears_old_result(qtbot, tmp_path, fleet):  # noqa: F811
    window, run = run_card(qtbot, tmp_path, fleet[:1])
    window.state.stand.remove_camera(1)
    window.menu.setCurrentRow(0)
    window.menu.setCurrentRow(3)
    assert not run.cameras.rows and run.protocol_path() is None


def test_failed_publishing_does_not_offer_previous_protocol(
    qtbot,
    tmp_path,
    fleet,  # noqa: F811
    monkeypatch,
):
    window, run = run_card(qtbot, tmp_path, fleet[:1])
    run.warn = lambda *_: None
    monkeypatch.setattr("qa_tool.report.publish.publish", lambda _store: 1 / 0)
    window.run_block.start_button.click()
    finish(qtbot, window)
    assert run.protocol_path() is None
    assert not run.card.tasks.protocol_link.isEnabled()
    assert "протоколы не выпущены" in window.run_block.status.text()


def test_s11_camera_text_in_tooltips_and_warnings_stays_text(qtbot):
    create_app()
    assert safe_tip("<h1 style='color:red'>x</h1>") == (
        "<span>&lt;h1 style=&#x27;color:red&#x27;&gt;x&lt;/h1&gt;</span>"
    )
    assert safe_tip("") == ""


def test_s13_run_ended_while_question_was_open(qtbot, tmp_path, fleet):  # noqa: F811
    window, _ = running_window(qtbot, tmp_path, fleet[:1])

    def finished_meanwhile(_parent, _closing):
        window.run_control.stop()
        finish(qtbot, window)
        return True

    window.ask_stop = finished_meanwhile
    assert window.close() is True
    assert not window.close_after_run


def test_sync_notice_and_progress_line_are_shown(qtbot, tmp_path, fleet):  # noqa: F811
    from qa_tool.core.scheduler import Event
    from qa_tool.gui.help import HELP_ID

    window = make_window(qtbot, tmp_path, fleet[:1], registry_ref=FAKE, checks=["FAKE-PASSPORT-1"])
    run = screen(window, RunScreen)
    window.menu.setCurrentRow(3)
    assert not run.notice.isVisibleTo(window)
    run._on_event(Event("phase", title="синхронная стенда", phase=2))
    assert run.notice.isVisibleTo(window)
    assert run.notice.property(HELP_ID) == "run-sync" and run.notice.toolTip()
    run._on_event(Event("started", 1, "IMAGE-EFFECT-1", "Картинка: эффект настроек"))
    run._on_event(Event("progress", 1, "IMAGE-EFFECT-1", detail="Контраст · 50 % · 2 из 9"))
    run.card.show_view(run.view.slots[1])
    texts = [w.text() for w in run.card.steps.body.findChildren(type(run.notice))]
    assert any(t.startswith("Контраст · 50 % · 2 из 9 · 0:0") for t in texts), texts
    run._on_event(Event("finished", detail="QA-1"))
    assert not run.notice.isVisibleTo(window)
