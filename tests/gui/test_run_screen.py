import pytest
from PySide6.QtCore import Qt

from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow, format_elapsed, stop_question
from qa_tool.gui.screens.run import RunScreen
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
