import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import events
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.soap import Exchange, OnvifFault, SoapClient
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(events.CHECK_ID)


@pytest.fixture
def camera(request):
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(**request.param)).start()
    yield cam
    cam.stop()


def run(cam):
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    return run_check(
        SPEC, CheckContext(camera=target, password="admin", place=Place.COLOR_BOX, extra={})
    )


@pytest.mark.parametrize(
    "camera",
    [
        {},
        {"subscribe_needs_addressing": True},
        {"subscribe_reference": True},
    ],
    ids=["обычная", "нужен WS-Addressing", "параметры ссылки"],
    indirect=True,
)
def test_full_subscription_cycle_passes(camera):
    result = run(camera)
    assert result.status is Status.PASSED, result.reason
    assert result.evidence
    assert camera.state.notify_until is None  # камера осталась без подписки


@pytest.mark.parametrize(
    ("camera", "reason"),
    [
        ({"subscribe_fault": True}, "не приняла подписку"),
        ({"renew_keeps_time": True}, "Renew не продлил"),
        ({"unsubscribe_ignored": True}, "живёт после Unsubscribe"),
        ({"subscribe_address": "http://10.9.9.9/onvif/notify"}, "чужом хосте"),
    ],
    ids=["отказ", "срок не продлён", "отмена не сработала", "чужой хост"],
    indirect=["camera"],
)
def test_defects_of_camera_are_failures(camera, reason):
    result = run(camera)
    assert result.status is Status.FAILED
    assert reason in result.reason


@pytest.mark.parametrize("camera", [{"renew_keeps_time": True}], indirect=True)
def test_subscription_is_removed_after_failed_check(camera):
    run(camera)
    assert camera.state.notify_until is None


def test_camera_gone_is_a_stand_error():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState()).start()
    cam.stop()
    assert run(cam).status is Status.STAND_ERROR


def test_consumer_address_is_reserved_for_documentation():
    assert events.CONSUMER.startswith("http://192.0.2.")  # RFC 5737: туда никто не ответит


@pytest.mark.parametrize("camera", [{}], indirect=True)
def test_refusal_to_log_in_after_unsubscribe_proves_nothing(camera, monkeypatch):
    real = SoapClient.call

    def call(self, url, body, soap_header=""):
        if "Renew" in body and camera.state.notify_until is None:
            raise OnvifFault("denied", code="ter:NotAuthorized")
        return real(self, url, body, soap_header)

    monkeypatch.setattr(SoapClient, "call", call)
    assert run(camera).status is Status.STAND_ERROR


@pytest.mark.parametrize("camera", [{}], indirect=True)
def test_refused_get_services_is_a_failure_with_evidence(camera, monkeypatch):
    def refuse(soap, *_):
        soap.exchanges.append(Exchange("u", "запрос", 500, "ответ"))
        raise OnvifFault("ter:Action")

    monkeypatch.setattr(events, "service_urls", refuse)
    result = run(camera)
    assert result.status is Status.FAILED and result.evidence


@pytest.mark.parametrize("camera", [{"foreign_services": ("events",)}], indirect=True)
def test_events_on_foreign_host_are_named(camera):
    result = run(camera)
    assert result.status is Status.SKIPPED
    assert "чужом хосте" in result.reason


@pytest.mark.parametrize("camera", [{}], indirect=True)
def test_answer_that_is_not_soap_after_unsubscribe_proves_nothing(camera, monkeypatch):
    real = SoapClient.call

    def call(self, url, body, soap_header=""):
        if "Renew" in body and camera.state.notify_until is None:
            raise OnvifFault("ответ не SOAP (HTTP 503)", status=503)
        return real(self, url, body, soap_header)

    monkeypatch.setattr(SoapClient, "call", call)
    assert run(camera).status is Status.STAND_ERROR
