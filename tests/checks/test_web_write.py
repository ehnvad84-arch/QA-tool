"""Запись через веб-интерфейс с галочкой «изменяющие»: изменение, сверка, возврат, предохранители."""

from qa_tool.checks import registry
from qa_tool.checks.web import write as write_module
from qa_tool.checks.web.walk import CHECK_ID, SUMMARY_ID
from qa_tool.core.checks import CheckContext, Severity, Status, run_check
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.core.web_journal import FOLDER_KEY, RUN_ID_KEY, load
from tests.mock_web import MockWebCamera

SPEC = registry().get(CHECK_ID)
FORM = """<h1>Изображение</h1>
<label>Яркость <input id="b" type="range" min="0" max="100" step="1" value="{{brightness}}"></label>
<label>Режим <select id="m"><option value="auto">Авто</option><option value="day">День</option>
</select></label><script>document.getElementById('m').value = '{{mode}}'</script>
<label>Зеркало <input id="f" type="checkbox"></label>
<script>document.getElementById('f').checked = '{{flip}}' === 'true'</script>
<label>Название <input id="n" type="text" value="{{name}}"></label>
<button onclick="fetch('/api/settings', {method: 'POST', body: JSON.stringify({
 brightness: document.getElementById('b').value, mode: document.getElementById('m').value,
 flip: String(document.getElementById('f').checked)})})">Сохранить</button>
<button onclick="fetch('/api/do-something', {method: 'POST'})">Действие</button>"""
HOME = '<h1>Камера</h1><a href="/image">Изображение</a>'
SETTINGS = {"brightness": "50", "mode": "auto", "flip": "false", "name": "cam"}
PASSPORT = {"manufacturer": "Hikvision"}


def context(camera, tmp_path, allow_writes=True, passport=PASSPORT, thresholds=None):
    port = int(camera.url.rsplit(":", 1)[1].strip("/"))
    target = Camera(slot=1, address="127.0.0.1", port=port, username="admin")
    extra = {FOLDER_KEY: str(tmp_path / "journal"), RUN_ID_KEY: "QA-1"}
    if passport is not None:
        extra["passport"] = passport
    return CheckContext(
        target,
        "secret",
        Place.COLOR_BOX,
        thresholds=thresholds or {},
        allow_writes=allow_writes,
        extra=extra,
    )


def run(camera, tmp_path, **options):
    return run_check(SPEC, context(camera, tmp_path, **options))


def mock(**options):
    return MockWebCamera(
        {"/": HOME, "/image": FORM}, auth="form", settings=dict(SETTINGS), **options
    )


def rows(result, title):
    return [r for r in result.children if r.title == title]


def writes_of(result):
    return [r for r in result.children if r.check_id.startswith("WEB-WRITE-")]


def summary(result):
    return next(r.result for r in result.children if r.check_id == SUMMARY_ID)


def journal(camera, tmp_path):
    port = int(camera.url.rsplit(":", 1)[1].strip("/"))
    return load(tmp_path / "journal", "127.0.0.1", port)


def test_settings_are_back_after_walk_and_journal_is_closed(tmp_path):
    with mock() as camera:
        result = run(camera, tmp_path)
    assert camera.settings == SETTINGS
    assert camera.saves >= 2
    written = writes_of(result)
    assert {r.title for r in written} == {"Яркость", "Режим Авто День", "Зеркало"}
    assert all(r.result.status is Status.PASSED for r in written), written
    assert journal(camera, tmp_path) is None
    assert "записей сделано 3, возвращено 3" in summary(result).actual


def test_free_text_is_not_changed(tmp_path):
    with mock() as camera:
        result = run(camera, tmp_path)
    assert rows(result, "Название")[0].result.status is Status.SKIPPED
    assert not any('"name"' in body for body in camera.bodies)  # логин — поле username


def test_not_saved_is_a_failure_with_declared_and_read_values(tmp_path):
    with mock() as camera:
        camera.saves_ignored = 1
        result = run(camera, tmp_path)
    first = writes_of(result)[0]
    assert first.result.status is Status.FAILED
    assert "заявлено" in first.result.actual and "прочитано" in first.result.actual
    assert camera.settings == SETTINGS


def test_action_buttons_stay_stopped_with_the_tick(tmp_path):
    with mock() as camera:
        run(camera, tmp_path)
    assert ("POST", "/api/do-something") not in camera.arrived


def test_no_writes_without_the_tick(tmp_path):
    with mock() as camera:
        result = run(camera, tmp_path, allow_writes=False)
    assert not writes_of(result) and camera.saves == 0


def test_ruver_passport_blocks_writes_whatever_the_case(tmp_path):
    for manufacturer in ("RUWARE", "ruware", "Рувер", "рувер"):
        with mock() as camera:
            result = run(camera, tmp_path, passport={"manufacturer": manufacturer})
        assert camera.saves == 0, manufacturer
        assert not writes_of(result)
        assert "«Рувер»" in rows(result, "Яркость")[0].result.reason


def test_ruver_writes_with_the_second_permission(tmp_path):
    allowed = {"web.ruver_writes": Threshold(1, "", "тест")}
    with mock() as camera:
        run(camera, tmp_path, passport={"manufacturer": "RUWARE"}, thresholds=allowed)
    assert camera.saves >= 2 and camera.settings == SETTINGS


def test_unread_passport_means_no_writes(tmp_path):
    with mock() as camera:
        result = run(camera, tmp_path, passport=None)
    assert camera.saves == 0
    assert "паспорт" in rows(result, "Яркость")[0].result.reason


def test_writes_limit_is_respected(tmp_path):
    limit = {"web.writes_limit": Threshold(1, "шт", "тест")}
    with mock() as camera:
        result = run(camera, tmp_path, thresholds=limit)
    assert len(writes_of(result)) == 1


def test_camera_asking_for_reboot_is_not_rebooted_and_row_is_skipped(tmp_path):
    form = FORM + "<p>Для применения нужна перезагрузка камеры</p>"
    with MockWebCamera({"/": HOME, "/image": form}, auth="form", settings=dict(SETTINGS)) as camera:
        result = run(camera, tmp_path)
    assert camera.settings == SETTINGS
    assert all(r.result.status is Status.SKIPPED for r in writes_of(result))
    assert "без перезагрузки" in writes_of(result)[0].result.reason


def test_value_not_returned_is_a_blocker_and_stops_writing(tmp_path):
    with mock() as camera:
        camera.ignore_all_saves_after = 1  # новое значение принято, возврат — нет
        result = run(camera, tmp_path)
    first = writes_of(result)[0]
    assert first.result.status is Status.FAILED and first.severity is Severity.BLOCKER
    assert "осталось" in first.result.actual
    assert len(writes_of(result)) == 1
    assert journal(camera, tmp_path) is not None


def test_adapter_reads_written_address_when_it_is_known(tmp_path, monkeypatch):
    asked = []

    def reader(_ctx, url):
        asked.append(url)
        return "другое значение"

    monkeypatch.setitem(write_module.API_READERS, "generic", reader)
    with mock() as camera:
        result = run(camera, tmp_path)
    assert any("/api/settings" in url for url in asked)
    assert writes_of(result)[0].result.status is Status.FAILED
    assert camera.settings == SETTINGS  # и при расхождении значение возвращено
