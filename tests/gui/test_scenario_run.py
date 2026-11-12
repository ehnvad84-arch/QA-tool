import pytest
from PySide6.QtCore import Qt

from qa_tool.core.config import load_scenario
from qa_tool.core.models import Camera
from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.screens.run import RunScreen
from qa_tool.gui.screens.scenario import ScenarioScreen
from qa_tool.gui.state import AppState
from tests.fake_checks import SLOW
from tests.mock_camera import start_fleet


@pytest.fixture
def fleet(monkeypatch):
    cameras, _ = start_fleet(8)
    for slot in range(1, 9):
        monkeypatch.setenv(f"QA_TOOL_PASSWORD_{slot}", "admin")
    yield cameras
    for cam in cameras:
        cam.stop()


def make_window(qtbot, tmp_path, fleet, registry_ref="qa_tool.checks:registry", checks=None):
    create_app()
    state = AppState(stand_path=tmp_path / "stand.yaml", registry_ref=registry_ref)
    for slot, m in enumerate(fleet, start=1):
        state.stand.add_camera(
            Camera(
                slot=slot, address="127.0.0.1", port=m.port, rtsp_port=m.rtsp_port, username="admin"
            )
        )
    state.scenario.checks = checks
    state.save()
    window = MainWindow(state)
    qtbot.addWidget(window)
    window.show()
    return window


def screen(window, cls):
    return next(s for s in window.screens if isinstance(s, cls))


def test_run_eight_cameras_from_window(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet, checks=["CORE-CONNECT-1"])
    window.menu.setCurrentRow(3)
    run = screen(window, RunScreen)
    window.run_block.start_button.click()
    assert window.state.running and "идёт прогон" in window.status.text()
    qtbot.waitUntil(lambda: not window.state.running, timeout=60000)
    for slot in run.view.slots.values():
        assert slot.counts["прошла"] == 1, slot.summary
        assert slot.percent == 100
    run_dir = window.run_control.last_store.run_dir
    assert (run_dir / "summary.pdf").exists() and (run_dir / "protocol_8.xlsx").exists()


def test_stop_releases_protocols_for_what_was_done(qtbot, tmp_path, fleet):
    window = make_window(
        qtbot, tmp_path, fleet[:2], registry_ref="tests.fake_checks:REGISTRY", checks=SLOW
    )
    run = screen(window, RunScreen)
    control = window.run_control
    trace = tmp_path / "trace"
    trace.mkdir()
    window.run_block.start_button.click()
    control.runner.extra["trace_dir"] = str(trace)
    qtbot.waitUntil(lambda: any(s.counts["прошла"] for s in run.view.slots.values()), timeout=30000)
    control.stop()
    qtbot.waitUntil(lambda: not window.state.running, timeout=30000)
    store = control.last_store
    statuses = {r["status"] for r in store.results()}
    assert "пропущена" in statuses
    assert store.run_info()["state"] == "остановлен"
    assert (store.run_dir / "summary.html").exists()


def test_scenario_choices_saved(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet[:1], registry_ref="tests.fake_checks:REGISTRY")
    scenario = screen(window, ScenarioScreen)
    window.menu.setCurrentRow(2)
    assert not scenario.allow_destructive.isChecked()
    item = next(
        i for i in scenario._check_items() if i.data(0, Qt.ItemDataRole.UserRole) == "FAKE-NET-1"
    )
    item.setCheckState(0, Qt.CheckState.Unchecked)
    scenario.allow_writes.setChecked(True)
    saved = load_scenario(tmp_path / "scenario.yaml")
    assert "FAKE-NET-1" not in saved.checks and "FAKE-NET-2" in saved.checks
    assert saved.allow_writes and not saved.allow_destructive


def test_protocols_screen_opens_all_formats(qtbot, tmp_path, fleet):
    from qa_tool.gui.screens.protocols import ProtocolsScreen

    window = make_window(qtbot, tmp_path, fleet[:2], checks=["CORE-CONNECT-1"])
    window.run_block.start_button.click()
    qtbot.waitUntil(lambda: not window.state.running, timeout=60000)

    protocols = screen(window, ProtocolsScreen)
    opened = []
    protocols.opener = opened.append
    window.menu.setCurrentRow(4)
    assert protocols.runs_table.rowCount() == 1
    assert protocols.docs_table.rowCount() == 3  # сводка + 2 камеры
    for row in range(3):
        protocols.docs_table.selectRow(row)
        for fmt in ("pdf", "xlsx", "html"):
            assert protocols.open_selected(fmt) is not None
    names = sorted(p.name for p in opened)
    assert names == sorted(
        f"{stem}.{fmt}"
        for stem in ("summary", "protocol_1", "protocol_2")
        for fmt in ("pdf", "xlsx", "html")
    )


def test_stress_duration_chosen_from_ten_minute_steps(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet[:1], registry_ref="tests.fake_checks:REGISTRY")
    scenario = screen(window, ScenarioScreen)
    values = [scenario.stress.itemData(i) for i in range(scenario.stress.count())]
    assert values[:5] == [10, 20, 30, 40, 50] and values[-1] == 1440
    assert scenario.stress.itemText(values.index(240)) == "4 ч"
    scenario.stress.setCurrentIndex(values.index(40))
    assert load_scenario(tmp_path / "scenario.yaml").stress_minutes == 40


def test_stress_checkbox_unlocks_duration_and_is_saved(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet[:1], registry_ref="tests.fake_checks:REGISTRY")
    scenario = screen(window, ScenarioScreen)
    assert not scenario.allow_stress.isChecked()
    assert not scenario.stress.isEnabled()
    assert scenario.allow_stress.toolTip()
    scenario.allow_stress.setChecked(True)
    assert scenario.stress.isEnabled()
    assert load_scenario(tmp_path / "scenario.yaml").allow_stress


def test_onvif_groups_tooltips_and_f1(qtbot, tmp_path, fleet):
    from qa_tool.checks import registry
    from qa_tool.gui.help import HELP_ID
    from qa_tool.gui.help_window import load_topics

    window = make_window(qtbot, tmp_path, fleet[:1])
    scenario = screen(window, ScenarioScreen)
    onvif = next(
        scenario.tree.topLevelItem(i)
        for i in range(scenario.tree.topLevelItemCount())
        if scenario.tree.topLevelItem(i).text(0) == "ONVIF"
    )
    groups = {onvif.child(i).text(0): onvif.child(i) for i in range(onvif.childCount())}
    assert {"Тесты onvif-tt", "Профиль G: накопитель и архив", "Итог по профилям"} <= set(groups)
    archive = groups["Профиль G: накопитель и архив"]
    ids = {archive.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(archive.childCount())}
    assert ids == {"RUV-SD-1", "RUV-SD-2", "RUV-G-SEARCH-1", "RUV-G-REPLAY-1"}
    replay = next(
        i
        for i in scenario._check_items()
        if i.data(0, Qt.ItemDataRole.UserRole) == "RUV-G-REPLAY-1"
    )
    assert "G: Воспроизведение архива" in replay.toolTip(0)
    assert len(scenario._check_items()) == len(registry().all())  # группы — не проверки

    scenario.tree.setCurrentItem(replay)
    assert scenario.tree.property(HELP_ID) == "onvif-archive"
    assert window.show_help(scenario.tree).heading.startswith("Профиль G")
    topics = load_topics()
    assert {s.help_id for s in registry().all() if s.help_id} <= set(topics)

    archive.setCheckState(0, Qt.CheckState.Unchecked)  # галочка группы снимает все её проверки
    saved = load_scenario(tmp_path / "scenario.yaml")
    assert not {"RUV-SD-1", "RUV-SD-2", "RUV-G-REPLAY-1"} & set(saved.checks)
    assert "RUV-M-STREAM-1" in saved.checks


def test_connect_check_cannot_be_unchecked(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet[:1], checks=["RUV-S-STREAM-1"])
    scenario = screen(window, ScenarioScreen)
    item = next(
        i
        for i in scenario._check_items()
        if i.data(0, Qt.ItemDataRole.UserRole) == "CORE-CONNECT-1"
    )
    assert item.checkState(0) == Qt.CheckState.Checked
    assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable


def test_image_note_under_writes_checkbox_follows_it(qtbot, tmp_path, fleet):
    from qa_tool.gui.help import HELP_ID

    window = make_window(qtbot, tmp_path, fleet[:1])
    scenario = screen(window, ScenarioScreen)
    window.menu.setCurrentRow(2)
    note = scenario.writes_note
    assert note.isVisibleTo(window) and note.text().startswith("Без этой галочки «Картинка»")
    assert note.property(HELP_ID) == "scenario-permissions" and note.toolTip()
    assert "«Картинка» снимает текущий кадр" in scenario.allow_writes.toolTip()
    scenario.allow_writes.setChecked(True)
    assert note.text().startswith("«Картинка» будет менять")
    item = next(
        i
        for i in scenario._check_items()
        if i.data(0, Qt.ItemDataRole.UserRole) == "IMAGE-EFFECT-1"
    )
    assert item.parent().text(0) == "Настройки изображения"
    assert item.data(1, Qt.ItemDataRole.UserRole) == "image"
    assert "Без этой галочки" in item.toolTip(0)
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert "«Картинка»" not in note.text()


def test_codec_note_follows_writes_checkbox_and_sits_in_its_group(qtbot, tmp_path, fleet):
    window = make_window(qtbot, tmp_path, fleet[:1])
    scenario = screen(window, ScenarioScreen)
    window.menu.setCurrentRow(2)
    note = scenario.writes_note
    assert "Без этой галочки «Кодеки» только прочитают" in note.text()
    assert "«Кодеки» — текущие настройки кодировщиков" in scenario.allow_writes.toolTip()
    scenario.allow_writes.setChecked(True)
    assert "«Кодеки» будут менять кодек и битрейт" in note.text()
    item = next(
        i
        for i in scenario._check_items()
        if i.data(0, Qt.ItemDataRole.UserRole) == "CODEC-QUALITY-1"
    )
    assert item.parent().text(0) == "Качество сжатия"
    assert item.data(1, Qt.ItemDataRole.UserRole) == "codec"


def test_protocols_screen_lists_codec_comparison(qtbot, tmp_path, fleet):
    from qa_tool.gui.screens.protocols import CODEC_COMPARE, ProtocolsScreen

    window = make_window(qtbot, tmp_path, fleet[:2], checks=["CORE-CONNECT-1"])
    window.run_block.start_button.click()
    qtbot.waitUntil(lambda: not window.state.running, timeout=60000)
    run_dir = window.run_control.last_store.run_dir
    (run_dir / f"{CODEC_COMPARE}.html").write_text("<p>сравнение</p>", encoding="utf-8")
    (run_dir / "compare").mkdir(exist_ok=True)
    (run_dir / "compare" / "codec_1.png").write_bytes(b"")
    protocols = screen(window, ProtocolsScreen)
    window.menu.setCurrentRow(4)
    protocols.refresh()
    assert protocols.docs_table.item(1, 0).text() == "Сравнение кодеков"
    assert protocols.docs_table.item(1, 2).text() == "разрешений и кодеков 1, камер 2"
    assert "Кривые качества" in protocols.docs_table.item(1, 0).toolTip()
    protocols.docs_table.selectRow(1)
    assert protocols.open_selected("html") == run_dir / f"{CODEC_COMPARE}.html"


def test_protocols_screen_lists_image_comparison(qtbot, tmp_path, fleet):
    from qa_tool.gui.screens.protocols import IMAGE_COMPARE, ProtocolsScreen

    window = make_window(qtbot, tmp_path, fleet[:2], checks=["CORE-CONNECT-1"])
    window.run_block.start_button.click()
    qtbot.waitUntil(lambda: not window.state.running, timeout=60000)
    run_dir = window.run_control.last_store.run_dir
    (run_dir / f"{IMAGE_COMPARE}.html").write_text("<p>сравнение</p>", encoding="utf-8")
    protocols = screen(window, ProtocolsScreen)
    opened = []
    protocols.opener = opened.append
    window.menu.setCurrentRow(4)
    protocols.refresh()
    assert protocols.docs_table.rowCount() == 4  # сводка, сравнение, 2 камеры
    assert protocols.docs_table.item(1, 0).text() == "Сравнение картинки"
    assert protocols.docs_table.item(1, 2).text() == "настроек 9, камер 2"
    assert "0, 50 и 100 %" in protocols.docs_table.item(1, 0).toolTip()
    protocols.docs_table.selectRow(1)
    assert protocols.open_selected("html") == run_dir / f"{IMAGE_COMPARE}.html"
