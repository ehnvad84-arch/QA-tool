from datetime import datetime

from qa_tool.core.models import Camera, Scenario
from qa_tool.core.scheduler import Event, Runner
from qa_tool.core.worker import LOST_DURING_CHECK, STOPPED
from qa_tool.gui.run_view import Dot, RunView, Step, plural
from tests.core.test_scheduler import REF, fleet, make_stand  # noqa: F401 — фикстура fleet

AT = datetime(2026, 11, 4, 9, 12)


def view_with(total=3, slots=8):
    view = RunView(slots)
    view.reset(1, Camera(slot=1, address="10.0.0.1", username="admin"), total)
    return view


def feed(view, *events):
    for e in events:
        view.apply(e, AT)
    return view.slots[1]


def test_plural_forms():
    assert [plural(n, "ошибка", "ошибки", "ошибок") for n in (1, 2, 5, 11, 21, 112)] == [
        "1 ошибка",
        "2 ошибки",
        "5 ошибок",
        "11 ошибок",
        "21 ошибка",
        "112 ошибок",
    ]


def test_s1_slot_states_before_during_after():
    view = view_with()
    slot = view.slots[1]
    assert (slot.dot, slot.summary, slot.maker) == (Dot.IDLE, "ожидание", "10.0.0.1")
    assert view.slots[2].summary == "свободно"
    feed(view, Event("started", 1, "CORE-CONNECT-1", "Камера отвечает"))
    assert (slot.dot, slot.summary) == (Dot.RUNNING, "подключение")
    feed(
        view,
        Event("passport", 1, passport={"manufacturer": "Dahua", "model": "X1", "firmware": "2"}),
        Event("result", 1, "CORE-CONNECT-1", "Камера отвечает", "прошла", severity="блокер"),
    )
    assert (slot.maker, slot.model, slot.percent, slot.summary) == (
        "Dahua",
        "Dahua X1",
        33,
        "ошибок нет",
    )
    feed(
        view,
        Event("result", 1, "A", "а", "прошла"),
        Event("result", 1, "B", "б", "прошла"),
    )
    assert (slot.dot, slot.percent) == (Dot.OK, 100)  # закончила раньше остальных камер
    feed(view, Event("finished"))
    assert (slot.dot, slot.percent) == (Dot.OK, 100)


def test_s3_step_log_with_time_and_composite_as_one_row():
    slot = feed(
        view_with(),
        Event("started", 1, "ONVIF-TT", "onvif-tt"),
        *[Event("result", 1, f"T-{i}", "т", "прошла", part=True) for i in range(160)],
        Event("result", 1, "T-X", "т", "пропущена", part=True),
        Event("done", 1, "ONVIF-TT", "onvif-tt"),
        Event("started", 1, "RUV-G-REPLAY-1", "Выдача архива"),
    )
    first, second = slot.steps
    assert (first.time, first.label, first.state) == ("09:12", "onvif-tt: 161 тест", "прошла")
    assert (second.label, second.state) == ("Выдача архива", "идёт")
    assert slot.done == 1 and slot.counts["прошла"] == 160


def test_s3_composite_takes_worst_result():
    slot = feed(
        view_with(),
        Event("started", 1, "ONVIF-TT", "onvif-tt"),
        Event("result", 1, "A", "а", "прошла", part=True),
        Event("result", 1, "B", "б", "не прошла", "нет ответа", part=True, severity="мелкая"),
        Event("done", 1, "ONVIF-TT", "onvif-tt"),
    )
    assert slot.steps[0].state == "не прошла"
    assert [t.check_id for t in slot.tasks] == ["B"]


def test_s4_tasks_only_failed_and_sorted_by_severity():
    slot = feed(
        view_with(total=5),
        Event("result", 1, "M", "мелкая", "не прошла", "m", severity="мелкая"),
        Event("result", 1, "S", "стенд", "ошибка стенда", "пароль", severity="блокер"),
        Event("result", 1, "B", "блокер", "не прошла", "b", severity="блокер"),
        Event("result", 1, "P", "пропуск", "пропущена", severity="блокер"),
        Event("result", 1, "J", "серьёзная", "не прошла", "j", severity="серьёзная"),
    )
    assert [t.check_id for t in slot.sorted_tasks()] == ["B", "J", "M"]
    assert (slot.dot, slot.summary) == (Dot.FAILED, "3 ошибки, ошибок стенда 1")


def test_s5_stand_error_is_yellow_and_not_a_task():
    slot = feed(
        view_with(),
        Event("started", 1, "CORE-CONNECT-1", "Камера отвечает"),
        Event("result", 1, "CORE-CONNECT-1", "т", "ошибка стенда", "неверный пароль"),
    )
    assert (slot.dot, slot.summary, slot.tasks) == (
        Dot.STAND,
        "ошибка стенда: неверный пароль",
        [],
    )


def test_s6_camera_stopped_responding_is_red_failure():
    slot = feed(
        view_with(),
        Event("started", 1, "X", "Запись идёт"),
        Event("result", 1, "X", "Запись идёт", "не прошла", LOST_DURING_CHECK, severity="блокер"),
        Event("lost", 1, detail="ошибка стенда: нет связи"),
    )
    assert (slot.dot, slot.summary) == (Dot.FAILED, "не отвечает")
    assert slot.steps[0].reason == LOST_DURING_CHECK
    assert [t.check_id for t in slot.tasks] == ["X"]


def test_lost_between_checks_closes_running_step_as_stand_error():
    slot = feed(
        view_with(),
        Event("started", 1, "X", "Запись идёт"),
        Event("lost", 1, detail="ошибка стенда: проверка зависла, камера снята с прогона"),
        Event("finished"),
    )
    assert slot.steps[0].state == "ошибка стенда"
    assert slot.dot == Dot.STAND and slot.summary.startswith("ошибка стенда: проверка зависла")
    assert slot.percent < 100


def test_task_order_matches_protocol(tmp_path, fleet):  # noqa: F811 — фикстура
    from qa_tool.report.model import build_protocol

    view = RunView(8)
    stand = make_stand(tmp_path, fleet[:1])
    checks = ["FAKE-PASSPORT-1", "FAKE-COMPOSITE-1", "FAKE-FAILS-1"]
    view.reset(1, stand.cameras[0], len(checks))
    runner = Runner(
        stand,
        Scenario(checks=checks),
        registry_ref=REF,
        on_event=lambda e: view.apply(e, AT),
    )
    store = runner.run()
    slot = view.slots[1]
    protocol = [t.title.split("]")[0].lstrip("[") for t in build_protocol(store, 1).tasks]
    assert [t.check_id for t in slot.sorted_tasks()] == protocol == ["SUB-2", "FAKE-FAILS-1"]
    assert slot.model == "Фейк <b>FK-1</b>" and slot.percent == 100


def test_stopped_run_is_not_shown_as_finished_without_errors():
    slot = feed(
        view_with(),
        Event("result", 1, "A", "а", "прошла"),
        Event("result", 1, "B", "б", "пропущена", STOPPED),
        Event("result", 1, "C", "в", "пропущена", STOPPED),
        Event("finished"),
    )
    assert (slot.dot, slot.percent, slot.summary) == (Dot.IDLE, 33, "остановлен, ошибок нет")


def test_skipped_by_scenario_is_still_a_finished_camera():
    slot = feed(
        view_with(total=2),
        Event("result", 1, "A", "а", "прошла"),
        Event("result", 1, "B", "б", "пропущена", "не разрешено сценарием"),
        Event("finished"),
    )
    assert (slot.dot, slot.percent, slot.summary) == (Dot.OK, 100, "ошибок нет")


def test_tasks_of_one_severity_are_ordered_by_check_id():
    slot = feed(
        view_with(),
        Event("result", 1, "Z-1", "я", "не прошла", severity="мелкая"),
        Event("result", 1, "A-1", "а", "не прошла", severity="мелкая"),
    )
    assert [t.check_id for t in slot.sorted_tasks()] == ["A-1", "Z-1"]


def test_tasks_of_one_severity_follow_protocol_phase_and_module():
    slot = feed(
        view_with(),
        Event("result", 1, "A-1", "а", "не прошла", severity="мелкая", phase=2, module="io"),
        Event("result", 1, "Z-1", "я", "не прошла", severity="мелкая", phase=1, module="onvif"),
        Event("result", 1, "Y-1", "ю", "не прошла", severity="мелкая", phase=1, module="core"),
    )
    assert [t.check_id for t in slot.sorted_tasks()] == ["Y-1", "Z-1", "A-1"]


def test_aborted_run_leaves_nothing_looking_alive():
    view = view_with()
    slot = feed(view, Event("started", 1, "X", "Запись"))
    view.abort("нет места на диске")
    assert slot.dot == Dot.STAND and slot.summary == "ошибка стенда: нет места на диске"
    assert slot.steps[0].state == "ошибка стенда"


def test_api_step_counts_operations():
    step = Step("09:12", "API-RUVER-1", "API «Рувер»", parts=["прошла"] * 300)
    assert step.label == "API «Рувер»: 300 операций"


def test_stream_step_counts_rows():
    step = Step("09:12", "STREAM-PARAMS-1", "Параметры потоков", parts=["прошла"] * 6)
    assert step.label == "Параметры потоков: 6 строк"


def test_image_progress_line_and_sync_phase():
    from datetime import timedelta

    view = RunView(2)
    view.reset(1, Camera(slot=1, address="10.0.0.5", username="admin"), 2)
    start = datetime(2026, 11, 9, 10, 0, 0)
    view.apply(Event("phase", title="синхронная стенда", phase=2), start)
    assert view.sync_phase
    view.apply(Event("started", 1, "IMAGE-EFFECT-1", "Картинка: эффект настроек"), start)
    later = start + timedelta(minutes=1, seconds=40)
    view.apply(Event("progress", 1, "IMAGE-EFFECT-1", detail="Контраст · 50 % · 2 из 9"), later)
    step = view.slots[1].steps[0]
    assert step.progress == "Контраст · 50 % · 2 из 9 · 1:40"
    view.apply(Event("progress", 1, "OTHER-1", detail="чужая"), later)
    assert step.progress.startswith("Контраст")  # ход чужой проверки не подменяет строку
    view.apply(Event("finished"), later)
    assert not view.sync_phase


def test_codec_step_counts_rows():
    step = Step("09:40", "CODEC-QUALITY-1", "Кодеки: качество на битрейт", parts=["прошла"] * 4)
    assert step.label == "Кодеки: качество на битрейт: 4 строки"


def test_image_step_counts_rows():
    step = Step("09:12", "IMAGE-EFFECT-1", "Картинка: эффект настроек", parts=["прошла"] * 11)
    assert step.label == "Картинка: эффект настроек: 11 строк"
