import datetime

from qa_tool.core.checks import CheckResult, CheckSpec, Evidence, Status
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.store import RunStore, next_run_id, safe_name


def make_stand(tmp_path):
    cams = [Camera(slot=i, address=f"10.0.0.{i}", username="admin") for i in (1, 2)]
    return Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=cams)


SPEC = CheckSpec("CORE-CONNECT-1", "core", "Связь", "описание", func=lambda c: None)


def test_run_id_format_and_increment(tmp_path):
    day = datetime.date(2026, 11, 3)
    assert next_run_id(tmp_path, day) == "QA-20261103-001"
    (tmp_path / "QA-20261103-001").mkdir()
    (tmp_path / "QA-20261103-007").mkdir()
    assert next_run_id(tmp_path, day) == "QA-20261103-008"


def test_results_written_and_read(tmp_path):
    store = RunStore.create(make_stand(tmp_path), Scenario())
    result = CheckResult(
        Status.PASSED,
        expected="порты отвечают",
        actual="ok",
        evidence=[Evidence("onvif-1.txt", "Authorization: Digest secret-hash\n<ok/>")],
        threshold=Threshold(5, "с", "стартовое"),
    )
    store.add_result(1, SPEC, result, started="2026-11-03T10:00:00")
    store.set_passport(
        1, {"manufacturer": "Ruver", "model": "M1", "firmware": "1.0", "serial": "S1"}
    )
    store.finish()
    rows = store.results(slot=1)
    assert [(r["check_id"], r["status"], r["threshold_source"]) for r in rows] == [
        ("CORE-CONNECT-1", "прошла", "стартовое")
    ]
    evidence_file = store.run_dir / rows[0]["evidence_dir"] / "onvif-1.txt"
    assert "secret-hash" not in evidence_file.read_text(encoding="utf-8")
    assert store.cameras()[0]["model"] == "M1"
    assert store.run_info()["state"] == "завершён"
    assert store.protocol_id(3) == f"{store.run_id}-3"


def test_no_password_column_in_cameras(tmp_path):
    store = RunStore.create(make_stand(tmp_path), Scenario())
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(cameras)")}
    assert not columns & {"password", "пароль"}


def test_evidence_name_cannot_escape_folder(tmp_path):
    assert "/" not in safe_name("../../etc/passwd")
    assert not safe_name("../../etc/passwd").startswith(".")
    assert safe_name('a<b>:"c|?*.txt') == "a_b___c___.txt"
    store = RunStore.create(make_stand(tmp_path), Scenario())
    result = CheckResult(Status.FAILED, evidence=[Evidence("../../escape.txt", "x")])
    store.add_result(1, SPEC, result, started="t")
    assert not (tmp_path / "escape.txt").exists()
    assert list((store.evidence_root / "1").rglob("*escape.txt"))


def test_windows_reserved_names_get_prefix():
    assert safe_name("CON") == "_CON"
    assert safe_name("nul.txt") == "_nul.txt"
    assert safe_name("com1") == "_com1"


def test_broken_run_skipped_and_readonly_open_does_not_write(tmp_path):
    from qa_tool.gui.screens.protocols import list_runs

    good = RunStore.create(make_stand(tmp_path), Scenario())
    good.finish()
    good.close()
    broken = tmp_path / "runs" / "QA-20000101-001"
    broken.mkdir()
    (broken / "run.sqlite").write_bytes(b"not a database")
    before = (broken / "run.sqlite").read_bytes()
    runs = list_runs(tmp_path / "runs")
    assert [r["id"] for r in runs] == [good.run_id]
    assert (broken / "run.sqlite").read_bytes() == before
