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
    run.start_button.click()
    assert window.state.running and "идёт прогон" in window.status.text()
    qtbot.waitUntil(lambda: not window.state.running, timeout=60000)
    for tile in run.tiles.values():
        assert tile.tally["прошла"] == 1, tile.current.text()
        assert tile.progress.value() == tile.progress.maximum()
    run_dir = run.last_store.run_dir
    assert (run_dir / "summary.pdf").exists() and (run_dir / "protocol_8.xlsx").exists()


def test_stop_releases_protocols_for_what_was_done(qtbot, tmp_path, fleet):
    window = make_window(
        qtbot, tmp_path, fleet[:2], registry_ref="tests.fake_checks:REGISTRY", checks=SLOW
    )
    run = screen(window, RunScreen)
    trace = tmp_path / "trace"
    trace.mkdir()
    run.start_button.click()
    run.runner.extra["trace_dir"] = str(trace)
    qtbot.waitUntil(lambda: any(t.tally["прошла"] for t in run.tiles.values()), timeout=30000)
    run.stop_button.click()
    qtbot.waitUntil(lambda: not window.state.running, timeout=30000)
    statuses = {r["status"] for r in run.last_store.results()}
    assert "пропущена" in statuses
    assert run.last_store.run_info()["state"] == "остановлен"
    assert (run.last_store.run_dir / "summary.html").exists()


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
    run = screen(window, RunScreen)
    run.start_button.click()
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


def test_tile_counts_child_rows_but_progress_by_check(qtbot):
    from qa_tool.gui.screens.run import Tile

    create_app()
    tile = Tile(1)
    tile.reset(Camera(slot=1, address="a", username="u"), total=1)
    tile.on_result("прошла", advance=False)
    tile.on_result("не прошла", advance=False)
    tile.advance()
    assert tile.tally["прошла"] == 1 and tile.tally["не прошла"] == 1
    assert tile.progress.value() == tile.progress.maximum() == 1


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
    assert ids == {"RUV-SD-1", "RUV-SD-2", "RUV-G-REPLAY-1"}
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
