"""Тесты onvif-tt, которые QA-tool не запускает, — с причиной для протокола.

onvif-tt ходит к камере через посредника табу на 127.0.0.1 (AD-7). Тесты, которые сверяют
адрес камеры в ответах WS-Discovery с адресом подключения, против посредника ложно падают;
поиск камер по WS-Discovery QA-tool проверяет сам на экране «Камеры» и в CORE-CONNECT-1.

Тесты с меткой onvif-tt `requires_ffprobe` запускают ffprobe с логином и паролем в адресе
RTSP (`rtsp://user:пароль@хост`): пароль виден в списке процессов компьютера стенда, а хост
берётся из ответа камеры без подмены — учётные данные могут уйти на чужой адрес. Посредник
табу RTSP не видит. Видеопоток вместо них проверяет RUV-S-STREAM-1 своим RTSP-клиентом;
список сверяется с метками onvif-tt тестом при каждом обновлении версии.

Изменяющие тесты doorcontrol могут выполнить команду у настоящей двери, а вернуть её состояние
снимок настроек не умеет (AD-3): служба бывает у устройств Profile D, QA-tool их не проверяет.
"""

NO_FFPROBE = (
    "запускает ffprobe с паролем камеры в адресе RTSP (виден в списке процессов стенда); "
    "видеопоток проверяет RUV-S-STREAM-1"
)
FFPROBE_TESTS = (
    "LOCAL-MEDIA-S-CODEC-MATCHES-PROFILE",
    "LOCAL-MEDIA-S-FRAME-RATE-WITHIN-TOLERANCE",
    "LOCAL-MEDIA-S-PTS-MONOTONIC",
    "LOCAL-MEDIA-S-RESOLUTION-MATCHES-PROFILE",
    "LOCAL-MEDIA-S-RTSP-LIVE",
    "LOCAL-MEDIA2-RTSP-LIVE",
    "LOCAL-RTSS-MULTIPLE-CONCURRENT-STREAMS",
    "LOCAL-RTSS-RTP-OVER-TCP-STREAM",
    "LOCAL-RTSS-RTP-OVER-UDP-STREAM",
    "LOCAL-RTSS-RTSP-KEEPALIVE",
)

NO_DOOR_RESTORE = (
    "выполняет команду двери, вернуть её состояние после теста нельзя; Profile D не проверяется"
)
DOOR_TESTS = tuple(f"DOORCONTROL-3-1-{n}" for n in range(19, 25))

EXCLUDED = {
    "LOCAL-DISCOVERY-PROBE": "сверяет адрес WS-Discovery с адресом подключения (через посредника "
    "это 127.0.0.1); поиск камер проверяет QA-tool",
    "DISCOVERY-2-1-1": "многоадресный Probe с компьютера стенда; поиск камер проверяет QA-tool",
    "DISCOVERY-1-1-2": "ждёт Hello с адресом подключения после перезагрузки (через посредника — "
    "127.0.0.1)",
}
EXCLUDED.update(dict.fromkeys(FFPROBE_TESTS, NO_FFPROBE))
EXCLUDED.update(dict.fromkeys(DOOR_TESTS, NO_DOOR_RESTORE))
