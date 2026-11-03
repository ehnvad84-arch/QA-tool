"""Службы ONVIF виртуальной камеры для этапа 2: GetServices и Scopes (объявленные профили),
Media2, поиск и воспроизведение записей, задания записи, SD-карта, метаданные, аналитика,
события PullPoint. Состояние — в памяти: включённое задание записи сразу даёт запись в поиске.
"""

import datetime
import itertools
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SERVICE_NS = {
    "device": "http://www.onvif.org/ver10/device/wsdl",
    "media": "http://www.onvif.org/ver10/media/wsdl",
    "media2": "http://www.onvif.org/ver20/media/wsdl",
    "events": "http://www.onvif.org/ver10/events/wsdl",
    "imaging": "http://www.onvif.org/ver20/imaging/wsdl",
    "recording": "http://www.onvif.org/ver10/recording/wsdl",
    "search": "http://www.onvif.org/ver10/search/wsdl",
    "replay": "http://www.onvif.org/ver10/replay/wsdl",
    "analytics": "http://www.onvif.org/ver20/analytics/wsdl",
}
ENVELOPE_NS = (
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
    'xmlns:tse="http://www.onvif.org/ver10/search/wsdl" '
    'xmlns:trp="http://www.onvif.org/ver10/replay/wsdl" '
    'xmlns:trc="http://www.onvif.org/ver10/recording/wsdl" '
    'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
    'xmlns:tan="http://www.onvif.org/ver20/analytics/wsdl" '
    'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
    'xmlns:wsa="http://www.w3.org/2005/08/addressing"'
)
PROFILE_SCOPES = {"S": "Streaming", "G": "G", "M": "M", "T": "T"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(element: ET.Element, local_name: str, default: str = "") -> str:
    """Текст первого потомка с таким локальным именем — без привязки к префиксам."""
    for child in element.iter():
        if child.tag.split("}")[-1] == local_name and child.text:
            return child.text.strip()
    return default


@dataclass
class OnvifState:
    """Что «умеет» и что «хранит» виртуальная камера."""

    profiles: tuple[str, ...] = ("S", "G", "M", "T")
    has_sd: bool = True
    sd_total_mb: int = 61_000
    sd_used_mb: int = 12_500
    recordings: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(
        default_factory=lambda: [{"token": "job-1", "recording": "rec-1", "mode": "Idle"}]
    )
    searches: dict[str, list[dict]] = field(default_factory=dict)
    subscriptions: set[str] = field(default_factory=set)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))


class OnvifServices:
    """Обработчики операций ONVIF; адреса служб — на той же машине, что и камера."""

    def __init__(self, state: OnvifState, base_url: str, rtsp_url: str):
        self.state = state
        self.base_url = base_url  # http://127.0.0.1:<порт>/onvif
        self.rtsp_url = rtsp_url  # rtsp://127.0.0.1:<порт>

    def handle(self, operation: str, request: ET.Element) -> str | None:
        """Тело ответа без конверта или None, если операция не поддержана."""
        handler = getattr(self, f"op_{operation}", None)
        return handler(request) if handler else None

    # --- Device -------------------------------------------------------------------------------

    def op_GetServices(self, _request) -> str:  # noqa: N802
        services = ["device", "media", "media2", "events", "imaging"]
        if "G" in self.state.profiles:
            services += ["recording", "search", "replay"]
        if "M" in self.state.profiles:
            services.append("analytics")
        items = "".join(
            f"<tds:Service><tds:Namespace>{SERVICE_NS[name]}</tds:Namespace>"
            f"<tds:XAddr>{self.base_url}/{name}_service</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>"
            for name in services
        )
        return f"<tds:GetServicesResponse>{items}</tds:GetServicesResponse>"

    def op_GetScopes(self, _request) -> str:  # noqa: N802
        scopes = [f"onvif://www.onvif.org/Profile/{PROFILE_SCOPES[p]}" for p in self.state.profiles]
        items = "".join(
            f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>"
            for s in scopes
        )
        return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"

    def op_GetStorageConfigurations(self, _request) -> str:  # noqa: N802
        if not self.state.has_sd:
            return "<tds:GetStorageConfigurationsResponse/>"
        return (
            "<tds:GetStorageConfigurationsResponse>"
            '<tds:StorageConfigurations token="sd-1"><tt:Data type="LocalStorage">'
            "<tt:LocalPath>/mnt/sd</tt:LocalPath></tt:Data></tds:StorageConfigurations>"
            "</tds:GetStorageConfigurationsResponse>"
        )

    # --- Media2 -------------------------------------------------------------------------------

    def op_GetProfiles(self, _request) -> str:  # noqa: N802
        metadata = (
            '<tr2:Configurations><tr2:Metadata token="meta-1"><tt:Name>Метаданные</tt:Name>'
            "</tr2:Metadata></tr2:Configurations>"
            if "M" in self.state.profiles
            else ""
        )
        return (
            '<tr2:GetProfilesResponse><tr2:Profiles token="main" fixed="true">'
            f"<tr2:Name>Основной</tr2:Name>{metadata}</tr2:Profiles></tr2:GetProfilesResponse>"
        )

    def op_GetStreamUri(self, _request) -> str:  # noqa: N802
        uri = f"{self.rtsp_url}/live"
        return f"<tr2:GetStreamUriResponse><tr2:Uri>{uri}</tr2:Uri></tr2:GetStreamUriResponse>"

    def op_GetMetadataConfigurations(self, _request) -> str:  # noqa: N802
        if "M" not in self.state.profiles:
            return "<tr2:GetMetadataConfigurationsResponse/>"
        return (
            '<tr2:GetMetadataConfigurationsResponse><tr2:Configurations token="meta-1">'
            "<tt:Name>Метаданные</tt:Name><tt:Analytics>true</tt:Analytics>"
            "</tr2:Configurations></tr2:GetMetadataConfigurationsResponse>"
        )

    # --- Analytics ----------------------------------------------------------------------------

    def op_GetSupportedAnalyticsModules(self, _request) -> str:  # noqa: N802
        return (
            "<tan:GetSupportedAnalyticsModulesResponse><tan:SupportedAnalyticsModules>"
            '<tt:AnalyticsModuleDescription Name="tt:CellMotionEngine"/>'
            '<tt:AnalyticsModuleDescription Name="tt:LineDetector"/>'
            "</tan:SupportedAnalyticsModules></tan:GetSupportedAnalyticsModulesResponse>"
        )

    def op_GetAnalyticsModules(self, _request) -> str:  # noqa: N802
        return (
            "<tan:GetAnalyticsModulesResponse>"
            '<tan:AnalyticsModule Name="Движение" Type="tt:CellMotionEngine"/>'
            "</tan:GetAnalyticsModulesResponse>"
        )

    # --- Управление записью --------------------------------------------------------------------

    def op_GetRecordingJobs(self, _request) -> str:  # noqa: N802
        items = "".join(
            f"<trc:JobItem><trc:JobToken>{j['token']}</trc:JobToken><trc:JobConfiguration>"
            f"<tt:RecordingToken>{j['recording']}</tt:RecordingToken><tt:Mode>{j['mode']}</tt:Mode>"
            "<tt:Priority>1</tt:Priority></trc:JobConfiguration></trc:JobItem>"
            for j in self.state.jobs
        )
        return f"<trc:GetRecordingJobsResponse>{items}</trc:GetRecordingJobsResponse>"

    def op_SetRecordingJobMode(self, request) -> str:  # noqa: N802
        token, mode = _text(request, "JobToken"), _text(request, "Mode")
        for job in self.state.jobs:
            if job["token"] == token:
                job["mode"] = mode
                if mode == "Active":  # виртуальная камера «записывает» мгновенно
                    start = _now() - datetime.timedelta(seconds=60)
                    self.state.recordings.append(
                        {"token": job["recording"], "start": start, "end": _now()}
                    )
        return "<trc:SetRecordingJobModeResponse/>"

    def op_GetRecordings(self, _request) -> str:  # noqa: N802
        items = "".join(
            f"<trc:RecordingItem><tt:RecordingToken>{r['token']}</tt:RecordingToken></trc:RecordingItem>"
            for r in self.state.recordings
        )
        return f"<trc:GetRecordingsResponse>{items}</trc:GetRecordingsResponse>"

    # --- Поиск и воспроизведение записей --------------------------------------------------------------------

    def op_FindRecordings(self, _request) -> str:  # noqa: N802
        token = f"search-{next(self.state._ids)}"
        self.state.searches[token] = list(self.state.recordings)
        return (
            f"<tse:FindRecordingsResponse><tse:SearchToken>{token}</tse:SearchToken>"
            "</tse:FindRecordingsResponse>"
        )

    def op_GetRecordingSearchResults(self, request) -> str:  # noqa: N802
        found = self.state.searches.get(_text(request, "SearchToken"), [])
        items = "".join(
            f"<tt:RecordingInformation><tt:RecordingToken>{r['token']}</tt:RecordingToken>"
            f"<tt:EarliestRecording>{_iso(r['start'])}</tt:EarliestRecording>"
            f"<tt:LatestRecording>{_iso(r['end'])}</tt:LatestRecording>"
            "<tt:RecordingStatus>Stopped</tt:RecordingStatus></tt:RecordingInformation>"
            for r in found
        )
        return (
            "<tse:GetRecordingSearchResultsResponse><tse:ResultList>"
            f"<tt:SearchState>Completed</tt:SearchState>{items}"
            "</tse:ResultList></tse:GetRecordingSearchResultsResponse>"
        )

    def op_EndSearch(self, request) -> str:  # noqa: N802
        self.state.searches.pop(_text(request, "SearchToken"), None)
        return (
            "<tse:EndSearchResponse><tse:Endpoint>1970-01-01T00:00:00Z</tse:Endpoint>"
            "</tse:EndSearchResponse>"
        )

    def op_GetReplayUri(self, request) -> str:  # noqa: N802
        token = _text(request, "RecordingToken")
        uri = f"{self.rtsp_url}/replay/{token}"
        return f"<trp:GetReplayUriResponse><trp:Uri>{uri}</trp:Uri></trp:GetReplayUriResponse>"

    # --- События (PullPoint) ---------------------------------------------------------------------

    def op_CreatePullPointSubscription(self, _request) -> str:  # noqa: N802
        sub = uuid.uuid4().hex[:8]
        self.state.subscriptions.add(sub)
        return (
            "<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
            f"<wsa:Address>{self.base_url}/events_service/sub/{sub}</wsa:Address>"
            f"</tev:SubscriptionReference><wsnt:CurrentTime>{_iso(_now())}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{_iso(_now() + datetime.timedelta(minutes=1))}"
            "</wsnt:TerminationTime></tev:CreatePullPointSubscriptionResponse>"
        )

    def op_PullMessages(self, _request) -> str:  # noqa: N802
        return (
            f"<tev:PullMessagesResponse><tev:CurrentTime>{_iso(_now())}</tev:CurrentTime>"
            f"<tev:TerminationTime>{_iso(_now() + datetime.timedelta(minutes=1))}"
            "</tev:TerminationTime></tev:PullMessagesResponse>"
        )

    def op_Unsubscribe(self, _request) -> str:  # noqa: N802
        return "<wsnt:UnsubscribeResponse/>"
