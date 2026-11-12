"""Протокол: задачи программистам без дублей."""

from qa_tool.report.model import Row, _without_repeats


def row(check_id, title="Обновить", reason="кнопка не отзывается", actual=""):
    return Row(
        number=1,
        check_id=check_id,
        module="Веб-интерфейс",
        title=title,
        description="",
        expected="",
        actual=actual,
        status="не прошла",
        reason=reason,
        threshold="",
        severity="мелкая",
        evidence="",
    )


def test_same_web_element_on_every_page_is_one_task():
    tasks = _without_repeats([row("WEB-ELEMENT-1"), row("WEB-ELEMENT-5"), row("WEB-ELEMENT-9")])
    assert len(tasks) == 1
    assert tasks[0][0].check_id == "WEB-ELEMENT-1"
    assert tasks[0][1] == ["WEB-ELEMENT-5", "WEB-ELEMENT-9"]


def test_different_web_elements_stay_separate():
    assert len(_without_repeats([row("WEB-ELEMENT-1"), row("WEB-ELEMENT-2", title="Вход")])) == 2


def test_rows_of_other_modules_are_not_merged():
    assert len(_without_repeats([row("EVENT-1"), row("EVENT-2")])) == 2
