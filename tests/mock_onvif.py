"""Службы ONVIF виртуальной камеры для этапа 2: GetServices и Scopes (объявленные профили),
Media2, поиск и воспроизведение записей, задания записи, SD-карта, метаданные, аналитика,
события PullPoint. Состояние — в памяти: включённое задание записи сразу даёт запись в поиске.
"""

import datetime
import itertools
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

SERVICE_NS = {
    "ptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "deviceio": "http://www.onvif.org/ver10/deviceIO/wsdl",
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
    'xmlns:wsa="http://www.w3.org/2005/08/addressing" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" '
    'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
)
PROFILE_SCOPES = {"S": "Streaming", "G": "G", "M": "M", "T": "T"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_xml(moment: datetime.datetime) -> str:
    return (
        f"<tt:UTCDateTime><tt:Date><tt:Year>{moment.year}</tt:Year><tt:Month>{moment.month}</tt:Month>"
        f"<tt:Day>{moment.day}</tt:Day></tt:Date><tt:Time><tt:Hour>{moment.hour}</tt:Hour>"
        f"<tt:Minute>{moment.minute}</tt:Minute><tt:Second>{moment.second}</tt:Second></tt:Time>"
        "</tt:UTCDateTime>"
    )


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
    # настройки, которые меняют изменяющие тесты onvif-tt (снимок и восстановление, T7)
    datetime_type: str = "NTP"
    dst: str = "false"
    tz: str = "MSK-3"
    imaging_xml: str = (
        "<tt:Brightness>50</tt:Brightness>"
        "<tt:Focus><tt:AutoFocusMode>MANUAL</tt:AutoFocusMode></tt:Focus>"
    )
    focus_position: str = "0.3"
    ptz_config_xml: str = (
        '<tptz:PTZConfiguration token="ptz-1"><tt:Name>PTZ</tt:Name>'
        "<tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout></tptz:PTZConfiguration>"
    )
    ptz_position: tuple[float, float, float] = (0.1, -0.2, 0.0)
    relay_xml: str = "<tt:Mode>Bistable</tt:Mode><tt:DelayTime>PT1S</tt:DelayTime><tt:IdleState>closed</tt:IdleState>"
    relay_state: str = "inactive"
    reject_set: bool = False  # камера отказывает в Set* — восстановление не удаётся
    replay_fault: bool = False  # камера объявляет Profile G, но GetReplayUri отказывает
    job_stuck_active: bool = False  # SetRecordingJobMode «OK», но из Active задание не выходит
    job_refuses_active: str = ""  # задание с этим токеном камера не включает
    imaging_without_settings: bool = False  # GetImagingSettings без ImagingSettings
    profile_token: str = "main"
    replay_require_unsupported: bool = False  # RTSP не знает «Require: onvif-replay» (551)
    replay_host: str = ""  # GetReplayUri отдаёт этот, внутренний адрес камеры (NAT)
    recording_stalled: bool = False  # задание Active, но архив не пополняется (карта сбоит)
    clock_offset_s: float = 0.0  # часы камеры отличаются от часов стенда
    storage_fault: bool = False  # камера не умеет GetStorageConfigurations
    metadata_stream: bool = True  # Profile M объявлен, но в потоке нет дорожки метаданных
    analytics_modules: tuple[str, ...] = ("tt:CellMotionEngine",)  # настроенные модули
    foreign_services: tuple[str, ...] = ()  # эти службы отдают внутренний адрес камеры (NAT)
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

    def _service_host(self, name: str) -> str:
        return "http://10.9.9.9" if name in self.state.foreign_services else self.base_url

    def op_GetServices(self, _request) -> str:  # noqa: N802
        services = ["device", "media", "media2", "events", "imaging", "ptz", "deviceio"]
        if "G" in self.state.profiles:
            services += ["recording", "search", "replay"]
        if "M" in self.state.profiles:
            services.append("analytics")
        items = "".join(
            f"<tds:Service><tds:Namespace>{SERVICE_NS[name]}</tds:Namespace>"
            f"<tds:XAddr>{self._service_host(name)}/{name}_service</tds:XAddr>"
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

    def op_GetStorageConfigurations(self, _request) -> str | None:  # noqa: N802
        if self.state.storage_fault:
            return None
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
            f"<tr2:GetProfilesResponse><tr2:Profiles token={quoteattr(self.state.profile_token)} "
            f'fixed="true"><tr2:Name>Основной</tr2:Name>{metadata}</tr2:Profiles>'
            "</tr2:GetProfilesResponse>"
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
        items = "".join(
            f'<tan:AnalyticsModule Name="Модуль {n}" Type="{kind}"/>'
            for n, kind in enumerate(self.state.analytics_modules, start=1)
        )
        return f"<tan:GetAnalyticsModulesResponse>{items}</tan:GetAnalyticsModulesResponse>"

    def op_GetAnalyticsConfigurations(self, _request) -> str:  # noqa: N802
        if "M" not in self.state.profiles:
            return "<tr2:GetAnalyticsConfigurationsResponse/>"
        return (
            '<tr2:GetAnalyticsConfigurationsResponse><tr2:Configurations token="va-1">'
            "<tt:Name>Аналитика</tt:Name></tr2:Configurations>"
            "</tr2:GetAnalyticsConfigurationsResponse>"
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

    def op_SetRecordingJobMode(self, request) -> str | None:  # noqa: N802
        token, mode = _text(request, "JobToken"), _text(request, "Mode")
        if mode == "Active" and token == self.state.job_refuses_active:
            return None
        for job in self.state.jobs:
            if job["token"] == token:
                if self.state.job_stuck_active and job["mode"] == "Active":
                    continue
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

    def op_GetRecordingSummary(self, _request) -> str:  # noqa: N802
        s = self.state
        ends = [r["end"] for r in s.recordings]
        if any(j["mode"] == "Active" for j in s.jobs) and not s.recording_stalled:
            ends.append(self._clock())  # запись идёт: последний кадр — «сейчас» по часам камеры
        starts = [r["start"] for r in s.recordings] or ends
        span = (
            f"<tt:DataFrom>{_iso(min(starts))}</tt:DataFrom><tt:DataUntil>{_iso(max(ends))}</tt:DataUntil>"
            if ends
            else ""
        )
        return (
            f"<tse:GetRecordingSummaryResponse><tse:Summary>{span}"
            f"<tt:NumberRecordings>{len(s.recordings)}</tt:NumberRecordings>"
            "</tse:Summary></tse:GetRecordingSummaryResponse>"
        )

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

    def op_GetReplayUri(self, request) -> str | None:  # noqa: N802
        if self.state.replay_fault:
            return None
        token = _text(request, "RecordingToken")
        uri = f"{self.rtsp_url}/replay/{token}"
        if self.state.replay_host:
            port = self.rtsp_url.rsplit(":", 1)[1]
            uri = f"rtsp://{self.state.replay_host}:{port}/replay/{token}"
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

    # --- настройки, которые меняют изменяющие тесты (T7) ----------------------------------------

    def _set_allowed(self) -> str | None:
        return "" if not self.state.reject_set else None

    def op_GetSystemDateAndTime(self, _request) -> str:  # noqa: N802
        s = self.state
        return (
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            f"<tt:DateTimeType>{s.datetime_type}</tt:DateTimeType>"
            f"<tt:DaylightSavings>{s.dst}</tt:DaylightSavings>"
            f"<tt:TimeZone><tt:TZ>{s.tz}</tt:TZ></tt:TimeZone>{_utc_xml(self._clock())}"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def _clock(self) -> datetime.datetime:
        return _now() + datetime.timedelta(seconds=self.state.clock_offset_s)

    def op_SetSystemDateAndTime(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        self.state.datetime_type = _text(request, "DateTimeType", self.state.datetime_type)
        self.state.dst = _text(request, "DaylightSavings", self.state.dst)
        self.state.tz = _text(request, "TZ", self.state.tz)
        return "<tds:SetSystemDateAndTimeResponse/>"

    def op_GetRelayOutputs(self, _request) -> str:  # noqa: N802
        return (
            '<tds:GetRelayOutputsResponse><tds:RelayOutputs token="relay-1">'
            f"<tt:Properties>{self.state.relay_xml}</tt:Properties>"
            "</tds:RelayOutputs></tds:GetRelayOutputsResponse>"
        )

    def op_SetRelayOutputSettings(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        properties = next(e for e in request if e.tag.endswith("Properties"))
        self.state.relay_xml = "".join(ET.tostring(c, encoding="unicode") for c in properties)
        return "<tds:SetRelayOutputSettingsResponse/>"

    def op_SetRelayOutputState(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        self.state.relay_state = _text(request, "LogicalState", self.state.relay_state)
        return "<tds:SetRelayOutputStateResponse/>"

    def op_GetVideoSources(self, _request) -> str:  # noqa: N802
        return '<trt:GetVideoSourcesResponse><trt:VideoSources token="vs-1"/></trt:GetVideoSourcesResponse>'

    def op_GetImagingSettings(self, _request) -> str:  # noqa: N802
        if self.state.imaging_without_settings:
            return "<timg:GetImagingSettingsResponse/>"
        return (
            "<timg:GetImagingSettingsResponse><timg:ImagingSettings>"
            f"{self.state.imaging_xml}</timg:ImagingSettings></timg:GetImagingSettingsResponse>"
        )

    def op_SetImagingSettings(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        settings = next(e for e in request if e.tag.endswith("ImagingSettings"))
        self.state.imaging_xml = "".join(ET.tostring(c, encoding="unicode") for c in settings)
        return "<timg:SetImagingSettingsResponse/>"

    def op_GetStatus(self, request) -> str:  # noqa: N802
        if any(e.tag.endswith("ProfileToken") for e in request):
            x, y, zoom = self.state.ptz_position
            return (
                "<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position>"
                f'<tt:PanTilt x="{x}" y="{y}"/><tt:Zoom x="{zoom}"/>'
                "</tt:Position></tptz:PTZStatus></tptz:GetStatusResponse>"
            )
        return (
            "<timg:GetStatusResponse><timg:Status><tt:FocusStatus20>"
            f"<tt:Position>{self.state.focus_position}</tt:Position>"
            "</tt:FocusStatus20></timg:Status></timg:GetStatusResponse>"
        )

    def op_Move(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        self.state.focus_position = _text(request, "Position", self.state.focus_position)
        return "<timg:MoveResponse/>"

    def op_GetConfigurations(self, _request) -> str:  # noqa: N802
        return f"<tptz:GetConfigurationsResponse>{self.state.ptz_config_xml}</tptz:GetConfigurationsResponse>"

    def op_SetConfiguration(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        config = next(e for e in request if e.tag.endswith("PTZConfiguration"))
        self.state.ptz_config_xml = ET.tostring(config, encoding="unicode")
        return "<tptz:SetConfigurationResponse/>"

    def op_AbsoluteMove(self, request) -> str | None:  # noqa: N802
        if self._set_allowed() is None:
            return None
        pan = next(e for e in request.iter() if e.tag.endswith("PanTilt"))
        zoom = next(e for e in request.iter() if e.tag.endswith("Zoom"))
        self.state.ptz_position = (float(pan.get("x")), float(pan.get("y")), float(zoom.get("x")))
        return "<tptz:AbsoluteMoveResponse/>"
