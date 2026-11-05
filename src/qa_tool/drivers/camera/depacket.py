"""Сборка RTP обратно в поток, который понимает декодер: H.264 и H.265 в формате Annex-B.

ffmpeg получает готовый файл, а не адрес камеры: адрес с паролем был бы виден в списке процессов.
Данные камеры недоверенные: длины проверяются, битая часть пакета просто отбрасывается, и о ней
скажет декодер.
"""

import base64
import binascii
import re
import struct

START_CODE = b"\x00\x00\x00\x01"

H264_STAP_A = 24
H264_FU_A = 28
H264_LAST_SINGLE = 23
H265_AP = 48
H265_FU = 49


def parameter_sets(fmtp: str) -> list[bytes]:
    """SPS, PPS (и VPS для H.265) из строки `a=fmtp`: часть камер шлёт их только в SDP.

    Args:
        fmtp: значение атрибута fmtp без номера формата.

    Returns:
        Наборы параметров в порядке, нужном декодеру; битый base64 пропускается.
    """
    found: dict[str, str] = {}
    for key, value in re.findall(r"([\w-]+)\s*=\s*([^;\s]+)", fmtp):
        found[key.lower()] = value
    values: list[str] = []
    for key in ("sprop-vps", "sprop-sps", "sprop-pps", "sprop-parameter-sets"):
        values += found.get(key, "").split(",") if key in found else []
    sets = []
    for value in values:
        try:
            sets.append(base64.b64decode(value + "=" * (-len(value) % 4), validate=True))
        except (binascii.Error, ValueError):
            continue
    return [s for s in sets if s]


def jpeg_size(payload: bytes) -> tuple[int, int] | None:
    """Ширина и высота из заголовка RTP JPEG (RFC 2435 §3.1); None — заголовка нет."""
    if len(payload) < 8 or not payload[6] or not payload[7]:
        return None
    return payload[6] * 8, payload[7] * 8


class Depacketizer:
    """Собирает пакеты одной дорожки; `start` — начало файла, `push` — пакет за пакетом.

    Args:
        encoding: кодек из SDP (H264, H265, HEVC).
        sets: наборы параметров из SDP.
    """

    def __init__(self, encoding: str, sets: list[bytes] | None = None):
        self.h265 = encoding.upper() in ("H265", "HEVC")
        self.sets = sets or []
        self._in_fragment = False

    def start(self) -> bytes:
        """Наборы параметров из SDP: без них декодер не прочитает камеру, что шлёт их только там."""
        return b"".join(START_CODE + s for s in self.sets)

    def push(self, payload: bytes) -> bytes:
        """Annex-B этого пакета; пусто — пакет не несёт кадра или он битый."""
        return self._push_h265(payload) if self.h265 else self._push_h264(payload)

    def _push_h264(self, payload: bytes) -> bytes:
        if not payload:
            return b""
        kind = payload[0] & 0x1F
        if kind == H264_FU_A:
            if len(payload) < 3:
                return b""
            header = bytes([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
            return self._fragment(payload[1], header, payload[2:])
        self._in_fragment = False
        if kind == H264_STAP_A:
            return _aggregated(payload[1:])
        return START_CODE + payload if 1 <= kind <= H264_LAST_SINGLE else b""

    def _push_h265(self, payload: bytes) -> bytes:
        if len(payload) < 3:
            return b""
        kind = (payload[0] >> 1) & 0x3F
        if kind == H265_FU:
            original = payload[2] & 0x3F
            header = bytes([(payload[0] & 0x81) | (original << 1), payload[1]])
            return self._fragment(payload[2], header, payload[3:])
        self._in_fragment = False
        if kind == H265_AP:
            return _aggregated(payload[2:])
        return START_CODE + payload if kind < H265_AP else b""

    def _fragment(self, flags: int, header: bytes, body: bytes) -> bytes:
        """Кусок FU: начало открывает NAL; продолжение без начала — потеря, его не склеить."""
        if flags & 0x80:
            self._in_fragment = not flags & 0x40
            return START_CODE + header + body
        if not self._in_fragment:
            return b""
        if flags & 0x40:
            self._in_fragment = False
        return body


def _aggregated(data: bytes) -> bytes:
    """NAL из пакета агрегации: у каждого спереди 2 байта длины."""
    out = bytearray()
    offset = 0
    while offset + 2 <= len(data):
        size = struct.unpack_from("!H", data, offset)[0]
        offset += 2
        if not size or offset + size > len(data):
            break
        out += START_CODE + data[offset : offset + size]
        offset += size
    return bytes(out)
