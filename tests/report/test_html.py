import pytest

from qa_tool.core.checks import CheckResult, CheckSpec, Evidence, Severity, Status
from qa_tool.core.models import Camera, Place, Scenario, Stand
from qa_tool.core.store import RunStore
from qa_tool.report.html import render_protocol, render_summary
from qa_tool.report.model import build_protocol, build_summary
from qa_tool.report.publish import publish


def spec(check_id, severity=Severity.MAJOR):
    return CheckSpec(
        check_id, "core", f"Проверка {check_id}", "что делаем", lambda c: None, severity=severity
    )


@pytest.fixture
def store(tmp_path):
    cams = [Camera(slot=i, address=f"10.0.0.{i}", username="admin") for i in (1, 2)]
    stand = Stand(place=Place.DARK_ROOM, runs_dir=tmp_path / "runs", cameras=cams)
    s = RunStore.create(stand, Scenario())
    s.set_passport(
        1,
        {
            "manufacturer": "Ruver",
            "model": "<script>alert(1)</script>",
            "firmware": "1.2.3",
            "serial": "SN1",
        },
    )
    ok = CheckResult(Status.PASSED, expected="e", actual="a", evidence=[Evidence("x.txt", "x")])
    s.add_result(1, spec("T-OK"), ok, "t")
    s.add_result(
        1,
        spec("T-BAD", Severity.BLOCKER),
        CheckResult(
            Status.FAILED,
            reason="password=s3cret не принят",
            expected="вход",
            actual="отказ <b>401</b>",
        ),
        "t",
    )
    s.add_result(1, spec("T-STAND"), CheckResult.stand_error("нет связи"), "t")
    s.add_result(1, spec("T-SKIP"), CheckResult.skipped("не разрешено сценарием"), "t")
    s.add_result(2, spec("T-OK"), ok, "t")
    s.finish()
    return s


def test_only_failed_checks_become_tasks(store):
    protocol = build_protocol(store, 1)
    assert [t.title.split("]")[0] for t in protocol.tasks] == ["[T-BAD"]
    assert protocol.tasks[0].severity == "блокер"
    assert protocol.verdict_kind == "incomplete"
    assert protocol.protocol_id == f"{store.run_id}-1"


def test_camera_answers_escaped_and_secrets_removed(store):
    html = render_protocol(build_protocol(store, 1))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>401</b>" not in html
    assert "s3cret" not in html
    assert "ПРОТОКОЛ ИСПЫТАНИЙ IP-КАМЕРЫ" in html and "тёмная комната" in html


def test_summary_matrix_covers_all_cameras(store):
    summary = build_summary(store)
    assert summary.matrix["T-OK"] == {1: "прошла", 2: "прошла"}
    assert summary.matrix["T-BAD"] == {1: "не прошла"}
    html = render_summary(summary)
    assert "СВОДНЫЙ ОТЧЁТ" in html and "Сравнение" in html


def test_publish_writes_protocol_per_camera_and_summary(store):
    names = {p.name for p in publish(store)}
    assert {"protocol_1.html", "protocol_2.html", "summary.html"} <= names


def pdf_text(path) -> str:
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument(None)
    assert doc.load(str(path)) == QPdfDocument.Error.None_
    return "".join(doc.getAllText(i).text() for i in range(doc.pageCount()))


def test_pdf_and_excel_published(store):
    from openpyxl import load_workbook

    names = {p.name for p in publish(store)}
    for slot in (1, 2):
        assert {f"protocol_{slot}.pdf", f"protocol_{slot}.xlsx"} <= names
    assert {"summary.pdf", "summary.xlsx"} <= names
    assert f"{store.run_id}-1" in pdf_text(store.run_dir / "protocol_1.pdf")
    assert store.run_id in pdf_text(store.run_dir / "summary.pdf")

    summary = load_workbook(store.run_dir / "summary.xlsx")
    assert summary.sheetnames == ["Сводка", "Проверки", "Задачи", "Сравнение"]
    matrix = list(summary["Сравнение"].values)
    assert matrix[0][2:] == ("1. Ruver <script>alert(1)</script>", "2. 10.0.0.2")
    row = next(r for r in matrix if r[0] == "T-OK")
    assert row[2:] == ("прошла", "прошла")
    camera = load_workbook(store.run_dir / "protocol_1.xlsx")
    assert camera.sheetnames == ["Сводка", "Проверки", "Задачи"]
    assert camera["Задачи"].max_row == 2  # заголовок + одна задача
