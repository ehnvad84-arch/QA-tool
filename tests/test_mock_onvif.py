import pytest

from qa_tool.drivers.camera.soap import NS, SoapClient
from tests.mock_camera import start_fleet
from tests.mock_onvif import OnvifState


@pytest.fixture
def cam():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def call(cam, body: str, service: str = "device_service"):
    return SoapClient("admin", "admin").call(f"http://127.0.0.1:{cam.port}/onvif/{service}", body)


def local(element, name):
    return [e for e in element.iter() if e.tag.split("}")[-1] == name]


def test_services_and_scopes_declare_profiles(cam):
    services = call(
        cam,
        "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>",
    )
    namespaces = {e.text for e in local(services, "Namespace")}
    assert "http://www.onvif.org/ver10/replay/wsdl" in namespaces
    assert "http://www.onvif.org/ver20/analytics/wsdl" in namespaces
    scopes = {e.text for e in local(call(cam, "<tds:GetScopes/>"), "ScopeItem")}
    assert (
        "onvif://www.onvif.org/Profile/Streaming" in scopes
        and "onvif://www.onvif.org/Profile/G" in scopes
    )


def test_media2_and_metadata(cam):
    assert local(call(cam, "<tr2:GetProfiles/>", "media2_service"), "Metadata")
    uri = local(call(cam, "<tr2:GetStreamUri/>", "media2_service"), "Uri")[0].text
    assert uri == f"rtsp://127.0.0.1:{cam.rtsp_port}/live"
    assert local(call(cam, "<tr2:GetMetadataConfigurations/>", "media2_service"), "Analytics")
    modules = local(
        call(cam, "<tan:GetSupportedAnalyticsModules/>", "analytics_service"),
        "AnalyticsModuleDescription",
    )
    assert len(modules) == 2


def test_recording_job_active_makes_recording_searchable_and_replayable(cam):
    search = lambda: local(  # noqa: E731
        call(
            cam,
            "<tse:GetRecordingSearchResults><tse:SearchToken>"
            + local(call(cam, "<tse:FindRecordings/>", "search_service"), "SearchToken")[0].text
            + "</tse:SearchToken></tse:GetRecordingSearchResults>",
            "search_service",
        ),
        "RecordingToken",
    )
    assert search() == []
    jobs = call(cam, "<trc:GetRecordingJobs/>", "recording_service")
    assert local(jobs, "Mode")[0].text == "Idle"
    call(
        cam,
        "<trc:SetRecordingJobMode><trc:JobToken>job-1</trc:JobToken>"
        "<trc:Mode>Active</trc:Mode></trc:SetRecordingJobMode>",
        "recording_service",
    )
    assert [e.text for e in search()] == ["rec-1"]
    uri = local(
        call(
            cam,
            "<trp:GetReplayUri><trp:RecordingToken>rec-1</trp:RecordingToken></trp:GetReplayUri>",
            "replay_service",
        ),
        "Uri",
    )[0].text
    assert uri.endswith("/replay/rec-1")


def test_storage_and_events(cam):
    assert local(call(cam, "<tds:GetStorageConfigurations/>"), "LocalPath")
    sub = local(call(cam, "<tev:CreatePullPointSubscription/>", "events_service"), "Address")[
        0
    ].text
    assert "/events_service/sub/" in sub
    assert local(call(cam, "<tev:PullMessages/>", "events_service"), "CurrentTime")


def test_camera_without_g_and_m():
    from tests.mock_camera import MockCamera, Passport

    cam = MockCamera(
        Passport("X", "M", "1", "S1"), state=OnvifState(profiles=("S",), has_sd=False)
    ).start()
    try:
        namespaces = {e.text for e in local(call(cam, "<tds:GetServices/>"), "Namespace")}
        assert "http://www.onvif.org/ver10/replay/wsdl" not in namespaces
        assert not local(call(cam, "<tds:GetStorageConfigurations/>"), "LocalPath")
        assert NS["tt"]
    finally:
        cam.stop()
