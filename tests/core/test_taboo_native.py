"""Табу чтения родного API (api-discovery, T1): у Dahua CGI запись и перезагрузка — обычный GET."""

import pytest

from qa_tool.core.taboo import TabooViolation, check_native_read

# Чтение, которое сборщики делали на живых камерах 09.11 (Hikvision .114, Dahua .108)
READS = [
    "/ISAPI/System/capabilities",
    "/ISAPI/System/deviceInfo",
    "/ISAPI/Image/channels/1",
    "/ISAPI/Image/channels/1/capabilities",
    "/ISAPI/Streaming/channels/101/capabilities",
    "/ISAPI/System/Video/inputs/channels/1/overlays/capabilities",
    "/ISAPI/System/Network/capabilities",
    "/ISAPI/Security/capabilities",
    "/cgi-bin/magicBox.cgi?action=getDeviceType",
    "/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
    "/cgi-bin/configManager.cgi?action=getConfig&name=VideoColor",
    "/cgi-bin/configManager.cgi?action=getConfig&name=Encode",
    "/cgi-bin/encode.cgi?action=getConfigCaps",
]

DANGERS = [
    "/cgi-bin/magicBox.cgi?action=reboot",  # Dahua: перезагрузка обычным GET
    "/cgi-bin/magicBox.cgi?action=shutdown",
    "/cgi-bin/magicBox.cgi?action=resetSystemEx",  # заводской сброс
    "/cgi-bin/configManager.cgi?action=setConfig&VideoColor[0][0].Brightness=0",
    "/cgi-bin/configManager.cgi?action=setDefault&name=All",
    "/cgi-bin/configManager.cgi?action=exportConfig",  # выгрузка конфигурации с паролями
    "/cgi-bin/configFileExport.backup",
    "/cgi-bin/eventManager.cgi?action=attach&codes=[All]",  # бесконечная подписка
    "/cgi-bin/upgrader.cgi?action=getState",  # обновление прошивки — не трогаем вовсе
    "/ISAPI/System/reboot",
    "/ISAPI/System/factoryReset?mode=basic",
    "/ISAPI/System/updateFirmware",
    "/ISAPI/System/configurationData",  # выгрузка конфигурации Hikvision
    "/ISAPI/ContentMgmt/Storage/hdd/1/format",
    "/cgi-bin/storageDevice.cgi?action=formatSD",
]


@pytest.mark.parametrize("path", READS)
def test_reads_of_live_samples_pass(path):
    check_native_read("GET", f"http://127.0.0.1:9108{path}")


@pytest.mark.parametrize("path", DANGERS)
def test_dangerous_gets_are_not_sent(path):
    with pytest.raises(TabooViolation):
        check_native_read("GET", f"http://127.0.0.1:9108{path}")


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE", "PATCH"])
def test_native_api_is_read_only(method):
    with pytest.raises(TabooViolation, match="только читается"):
        check_native_read(method, "http://127.0.0.1:9111/ISAPI/Image/channels/1/gain")


def test_network_and_user_writes_stay_forbidden_even_as_get():
    for path in (
        "/cgi-bin/configManager.cgi?action=setConfig&Network.eth0.IPAddress=1.2.3.4",
        "/cgi-bin/userManager.cgi?action=modifyPassword&name=admin&pwd=x&pwdOld=y",
    ):
        with pytest.raises(TabooViolation):
            check_native_read("GET", f"http://127.0.0.1:9108{path}")
