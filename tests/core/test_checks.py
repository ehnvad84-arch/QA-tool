import pytest

from qa_tool.core.checks import (
    CheckContext,
    CheckResult,
    Evidence,
    Phase,
    Registry,
    Status,
    run_check,
)
from qa_tool.core.models import Camera, Place, Scenario

RUVER_CAM = Camera(slot=1, address="10.0.0.1", username="admin", vendor="ruver")
OTHER_CAM = Camera(slot=2, address="10.0.0.2", username="admin", vendor="hikvision")


def ctx(camera=RUVER_CAM):
    return CheckContext(camera=camera, password="x", place=Place.COLOR_BOX)


def ok(_ctx):
    return CheckResult(Status.PASSED, evidence=[Evidence("ответ", "<ok/>")])


@pytest.fixture
def registry():
    reg = Registry()
    reg.check("T-NET-1", "core", "сетевая", "")(ok)
    reg.check("T-API-1", "api", "только Рувер", "", vendors={"ruver"})(ok)
    reg.check("T-NIGHT-1", "daynight", "ночь", "", phase=Phase.SYNC, places={Place.DARK_ROOM})(ok)
    reg.check("T-WRITE-1", "api", "меняет", "", writes=True)(ok)
    calls = []

    def destroy(_ctx):
        calls.append("called")
        return ok(_ctx)

    reg.check("T-RESET-1", "core", "сброс", "", destructive=True)(destroy)
    reg.destroy_calls = calls
    return reg


def plan_ids(plan):
    return {spec.id: reason for spec, reason in plan}


def test_destructive_check_skipped_and_not_called(registry):
    plan = plan_ids(registry.plan_for(RUVER_CAM, Scenario(), Place.COLOR_BOX))
    assert plan["T-RESET-1"] == "не разрешено сценарием: разрушающая проверка"
    spec = registry.get("T-RESET-1")
    result = run_check(spec, ctx(), skip=plan["T-RESET-1"])
    assert result.status is Status.SKIPPED
    assert result.reason == "не разрешено сценарием: разрушающая проверка"
    assert registry.destroy_calls == []


def test_destructive_check_runs_when_allowed(registry):
    plan = plan_ids(registry.plan_for(RUVER_CAM, Scenario(allow_destructive=True), Place.COLOR_BOX))
    assert plan["T-RESET-1"] is None


def test_writes_need_flag(registry):
    plan = plan_ids(registry.plan_for(RUVER_CAM, Scenario(), Place.COLOR_BOX))
    assert plan["T-WRITE-1"] == "не разрешено сценарием: изменяющая проверка"


def test_vendor_specific_checks_not_planned_for_competitor(registry):
    plan = plan_ids(registry.plan_for(OTHER_CAM, Scenario(), Place.COLOR_BOX))
    assert "T-API-1" not in plan
    assert "T-NET-1" in plan


def test_place_mismatch_is_skip_reason(registry):
    plan = plan_ids(registry.plan_for(RUVER_CAM, Scenario(), Place.COLOR_BOX))
    assert plan["T-NIGHT-1"] == "нужно другое место стенда: тёмная комната"


def test_scenario_check_list_filters(registry):
    plan = plan_ids(registry.plan_for(RUVER_CAM, Scenario(checks=["T-NET-1"]), Place.COLOR_BOX))
    assert list(plan) == ["T-NET-1"]


def test_plan_ordered_by_phase(registry):
    phases = [s.phase for s, _ in registry.plan_for(RUVER_CAM, Scenario(), Place.DARK_ROOM)]
    assert phases == sorted(phases)


def test_passed_without_evidence_becomes_stand_error():
    reg = Registry()
    reg.check("T-1", "core", "", "")(lambda _ctx: CheckResult(Status.PASSED))
    result = run_check(reg.get("T-1"), ctx())
    assert result.status is Status.STAND_ERROR
    assert "доказательство" in result.reason


def test_exception_in_check_is_stand_error():
    reg = Registry()

    def broken(_ctx):
        raise RuntimeError("нет кадра")

    reg.check("T-1", "core", "", "")(broken)
    result = run_check(reg.get("T-1"), ctx())
    assert result.status is Status.STAND_ERROR
    assert "нет кадра" in result.reason


def test_duplicate_id_rejected():
    reg = Registry()
    reg.check("T-1", "core", "", "")(ok)
    with pytest.raises(ValueError, match="уже зарегистрирована"):
        reg.check("T-1", "core", "", "")(ok)
