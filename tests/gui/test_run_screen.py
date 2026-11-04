from PySide6.QtCore import Qt

from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow, format_elapsed
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
