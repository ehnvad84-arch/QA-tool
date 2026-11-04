"""Собрать карту API «Рувер» `src/qa_tool/data/ruver_api_map.json` из описания прошивки.

Запуск: `uv run python scripts/gen_api_map.py [путь к openapi.yaml]`.

Метод, operationId и краткое описание не определяют, меняет ли операция настройку: чтение бывает
через POST, а описания неверны (`/api/recorder/remove_dir` описан как «Включить запись»). Поэтому
вид операции задают списки ниже, записанные руками. Новый POST вне списков получает вид
«не вызывается»: программа не шлёт камере то, что человек не разобрал (AD-7).

В репозиторий идёт наша карта, а не файл прошивки (never.md): `description` и `example`
отбрасываются, от схем остаются только правила проверки ответа.
"""

import json
import sys
from pathlib import Path

import yaml

from qa_tool.core.taboo import TabooViolation, check_http, check_web_request

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path.home() / "projects/hisi-fw-dev-gstack/www/openapi/doc/openapi.yaml"
TARGET = ROOT / "src/qa_tool/data/ruver_api_map.json"

READ, WRITE, TABOO, DESTRUCTIVE, NOT_CALLED, RETURN_ONLY = (
    "чтение",
    "запись",
    "табу",
    "разрушающая",
    "не вызывается",
    "только возврат",
)

AUTH_REASON = "служебный адрес входа: вызывается только при открытии сеанса"
MOTION = "движение объектива и механики нельзя вернуть точно"
NO_UNDO = "обратного хода нет: операция перезаписывает другие настройки"
NO_READ = "чтения на том же адресе нет: записанное значение нечем сверить"
NO_LIMITS = "описание не задаёт пределов значения: допустимое значение не подобрать"
COMPOSITE = "значение — текст или составная структура: подобрать его автоматически нельзя"
TIME_SOURCE = "приёмник ГЛОНАСС может быть источником времени камеры (AD-7)"

# Чтение через POST. Всё остальное с методом POST чтением не считается.
POST_READS = frozenset(
    {
        "/api/recorder/records",
        "/api/recorder/record_details",
        "/api/recorder/download",
        "/api/service/need_reboot",
        "/api/service/log",
        "/api/service/export_settings",
        "/api/video/snapshot",
    }
)

# GET, который не читает настройку или отказан фильтром табу. Остальные GET — чтение.
GET_NOT_READS = {
    "/api/auth/create_session": AUTH_REASON,
    "/api/auth/permissions": AUTH_REASON,
    "/api/users/get_password": "запрос пароля: программа пароли не читает (AD-7)",
    "/api/service/is_reset": "фильтр табу не пропускает слово «reset» в адресе (AD-7)",
}

# Решение директора 2026-11-04: перезагрузка — только под галочкой «разрушающие».
DESTRUCTIVE_OPS = frozenset(
    {"/api/service/reboot", "/api/sdcard/prepare", "/api/recorder/remove_dir"}
)

NOT_CALLED_POSTS = {
    "/api/auth/login": AUTH_REASON,
    "/api/auth/logout": AUTH_REASON,
    "/api/auth/validate_session": AUTH_REASON,
    "/api/ptz/zoom": MOTION,
    "/api/ptz/zoom_start": MOTION,
    "/api/ptz/zoom_stop": MOTION,
    "/api/ptz/focus": MOTION,
    "/api/ptz/focus_start": MOTION,
    "/api/ptz/focus_stop": MOTION,
    "/api/ptz/calibrate": MOTION,
    "/api/ptz/autofocus": MOTION,
    "/api/ptz/move": MOTION,
    "/api/ptz/move_stop": MOTION,
    "/api/ptz/goto_preset": MOTION,
    "/api/ptz/run_tour": MOTION,
    "/api/ptz/wiper": MOTION,
    "/api/ptz/named_presets": NO_UNDO,
    "/api/ptz/tours": NO_UNDO,
    "/api/image/reset_parameters": NO_UNDO,
    "/api/image/reset_all_parameters": NO_UNDO,
    "/api/image/preset_mode": NO_UNDO,
    "/api/osd/reset_overlay_object_positions": NO_UNDO,
    "/api/events/add": NO_UNDO,
    "/api/events/set_event": NO_UNDO,
    "/api/events/remove": NO_UNDO,
    "/api/events/remove_all": NO_UNDO,
    "/api/video/stream_mode": NO_UNDO,
    "/api/video/stream": "выбор потока в интерфейсе, а не настройка камеры",
    "/api/video/resolution": "допустимый список зависит от модели и потока",
    "/api/info": "название камеры может менять имя хоста (AD-7)",
    "/api/glonass/glonass_enable": TIME_SOURCE,
    "/api/glonass/sync_enable": TIME_SOURCE,
    "/api/glonass/sync_interval": TIME_SOURCE,
    "/api/glonass/synchronize": TIME_SOURCE,
    "/api/daynight/light_low": NO_READ,
    "/api/daynight/light_high": NO_READ,
    "/api/recorder/enable": NO_READ,
    "/api/recorder/settings": NO_READ,
    "/api/image/exposure/manual_shutter": NO_LIMITS,
    "/api/image/exposure/auto_gain_low": NO_LIMITS,
    "/api/image/exposure/auto_gain_high": NO_LIMITS,
    "/api/image/exposure/auto_shutter_low": NO_LIMITS,
    "/api/image/exposure/auto_shutter_high": NO_LIMITS,
    "/api/image/white_balance/temperature": NO_LIMITS,
    "/api/daynight/backlight": NO_LIMITS,
    "/api/video/roi": COMPOSITE,
    "/api/osd/privacy_zone": COMPOSITE,
    "/api/osd/custom_text": COMPOSITE,
    "/api/detector/roi": COMPOSITE,
    "/api/detector/fence": COMPOSITE,
    "/api/daynight/day_trigger": COMPOSITE,
    "/api/daynight/night_trigger": COMPOSITE,
}

# Сама по себе не проверяется: на TMID-018 0.6.14 запись сдвинула надпись, вернуть её можно только
# этой операцией (never.md: камера не остаётся изменённой). Разовый возврат разрешил директор.
RETURN_ONLY_OPS = frozenset({"/api/osd/set_block_position"})

WRITES = frozenset(
    {
        "/api/ptz/autofocus_enable",
        "/api/audio/volume",
        "/api/audio/codec",
        "/api/audio/input_channel",
        "/api/audio/mute_enable",
        "/api/image/mode",
        "/api/image/brightness",
        "/api/image/contrast",
        "/api/image/saturation",
        "/api/image/sharpness",
        "/api/image/compensation/wdr_mode",
        "/api/image/compensation/wdr_strength",
        "/api/image/compensation/hlc_enable",
        "/api/image/compensation/hlc_strength",
        "/api/image/compensation/blc_enable",
        "/api/image/compensation/blc_strength",
        "/api/image/compensation/hdr_enable",
        "/api/image/compensation/denoise_3dnr_enable",
        "/api/image/compensation/denoise_2dnr_enable",
        "/api/image/compensation/defog_mode",
        "/api/image/compensation/defog_strength",
        "/api/image/exposure/iris_enable",
        "/api/image/exposure/exposure_mode",
        "/api/image/exposure/manual_gain",
        "/api/image/exposure/auto_brightness",
        "/api/image/white_balance/mode",
        "/api/image/video_adjustment/corridor_mode",
        "/api/image/video_adjustment/transform",
        "/api/image/video_adjustment/antiflicker_mode",
        "/api/video/codec",
        "/api/video/profile",
        "/api/video/bitrate_mode",
        "/api/video/framerate",
        "/api/video/bitrate",
        "/api/video/frame_interval",
        "/api/video/roi_enable",
        "/api/osd/camera_name_enable",
        "/api/osd/glonass_enable",
        "/api/osd/date_enable",
        "/api/osd/privacy_enable",
        "/api/osd/meta_info_enable",
        "/api/osd/custom_text_enable",
        "/api/osd/font_size",
        "/api/daynight/mode",
        "/api/daynight/auto_backlight_enable",
        "/api/detector/detector_enable",
        "/api/detector/sensitivity",
        "/api/detector/mode",
        "/api/shock_sensor/shock_sensor_enable",
        "/api/shock_sensor/sensitivity",
    }
)

# Ответы могут содержать секреты: в доказательства идут только размер и хеш.
SENSITIVE_PREFIXES = (
    "/api/configuration",
    "/api/users",
    "/api/snmp",
    "/api/lte",
    "/api/network/https",
    "/api/service/log",
    "/api/service/export_settings",
)

# Значения меняются сами: при сверке «до и после» не сравниваются. Уточняется на живой камере.
LIVE_READS = frozenset(
    {
        "/api/datetime",
        "/api/daynight",
        "/api/daynight/light",
        "/api/daynight/backlight",
        "/api/glonass",
        "/api/glonass/coordinates",
        "/api/glonass/synchronization_state",
        "/api/lte",
        "/api/lte/info",
        "/api/ptz",
        "/api/ptz/zoom",
        "/api/ptz/focus",
        "/api/ptz/calibrating_state",
        "/api/ptz/focusing_state",
        "/api/ptz/running_tour_state",
        "/api/recorder",
        "/api/recorder/info",
        "/api/sdcard",
        "/api/sdcard/is_busy",
        "/api/users/auth_users",
    }
)

# Без значения в теле чтение не вызвать; адрес — настройка, которая есть на любой камере.
READ_BODIES = {"/api/service/need_reboot": {"route": "/api/image/brightness"}}

SCHEMA_KEYS = ("type", "enum", "format", "required", "minimum", "maximum")


class _Loader:
    """Читает описание, разбитое на файлы, и подставляет ссылки `$ref`."""

    def __init__(self) -> None:
        self._files: dict[Path, dict] = {}

    def load(self, path: Path) -> dict:
        path = path.resolve()
        if path not in self._files:
            self._files[path] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return self._files[path]

    def resolve(self, node, base: Path):
        if isinstance(node, list):
            return [self.resolve(item, base) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" not in node:
            return {key: self.resolve(value, base) for key, value in node.items()}
        file, _, fragment = node["$ref"].partition("#")
        target = (base.parent / file) if file else base
        found = self.load(target)
        for key in filter(None, fragment.split("/")):
            found = found[key]
        return self.resolve(found, target)


def plain_schema(schema: dict) -> dict:
    """Схема только с правилами проверки: `description`, `title` и `example` отброшены.

    Ключи разбираются по смыслу схемы, а не поиском по дереву: свойство ответа может
    называться `title` или `description`, и его нельзя потерять.
    """
    clean = {key: schema[key] for key in SCHEMA_KEYS if key in schema}
    if "properties" in schema:
        clean["properties"] = {k: plain_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        clean["items"] = plain_schema(schema["items"])
    for key in ("allOf", "oneOf", "anyOf"):
        if key in schema:
            clean[key] = [plain_schema(part) for part in schema[key]]
    return clean


def kind_of(method: str, path: str, has_read: bool) -> tuple[str, str]:
    """Вид операции и причина; табу определяет сам `core/taboo.py`, а не второй список."""
    if method == "GET":
        return (NOT_CALLED, GET_NOT_READS[path]) if path in GET_NOT_READS else (READ, "")
    try:
        check_http(method, path)
    except TabooViolation:
        return TABOO, "запрещено табу"
    if path in DESTRUCTIVE_OPS:
        return DESTRUCTIVE, "разрушающая операция"
    if path in POST_READS:
        return READ, ""
    if path in RETURN_ONLY_OPS:
        return RETURN_ONLY, "вызывается только для возврата сдвинутых надписей"
    if path in NOT_CALLED_POSTS:
        return NOT_CALLED, NOT_CALLED_POSTS[path]
    if path in WRITES and has_read:
        return WRITE, ""
    return NOT_CALLED, "новая операция: вид не разобран человеком"


def json_schema(content: dict | None) -> dict | None:
    """Схема из блока `content` ответа или запроса, если он в JSON."""
    media = (content or {}).get("application/json") or {}
    return media.get("schema")


def query_values(parameters: list[dict]) -> tuple[dict, list[str]]:
    """Значения обязательных параметров адреса: первое из перечня, иначе параметр не заполнить."""
    values, missing = {}, []
    for parameter in parameters:
        if parameter.get("in") != "query" or not parameter.get("required"):
            continue
        enum = (parameter.get("schema") or {}).get("enum")
        if enum:
            values[parameter["name"]] = enum[0]
        else:
            missing.append(parameter["name"])
    return values, missing


def write_field(body: dict | None, read: dict | None) -> dict:
    """Описание единственного поля записи: имя, тип, перечень, поле пределов в ответе чтения.

    Raises:
        ValueError: в список записи попала операция без поля, которое можно изменить и вернуть.
    """
    fields = (body or {}).get("properties") or {}
    if len(fields) != 1:
        raise ValueError(f"запись должна менять одно поле: {sorted(fields)}")
    name, schema = next(iter(fields.items()))
    range_field = f"{name}Range"
    read_fields = (read or {}).get("properties") or {}
    if name not in read_fields:
        raise ValueError(f"поля {name} нет в ответе чтения")
    if schema.get("enum"):
        return {"setting": name, "values": schema["enum"]}
    if schema.get("type") == "boolean":
        return {"setting": name, "boolean": True}
    if range_field in read_fields:
        return {"setting": name, "range_field": range_field}
    raise ValueError(f"у поля {name} нет пределов")


def section_of(path: str) -> str:
    """Раздел для идентификатора строки: `/api/shock_sensor/…` → SHOCK-SENSOR."""
    return path.split("/")[2].replace("_", "-").upper()


def build(source: Path) -> dict:
    """Собрать карту из описания прошивки.

    Args:
        source: путь к `openapi.yaml`.

    Returns:
        Версия описания и операции в порядке описания.
    """
    loader = _Loader()
    spec = loader.resolve(loader.load(source), source.resolve())
    counters: dict[str, int] = {}
    operations = []
    for path, item in spec["paths"].items():
        read_schema = json_schema(
            ((item.get("get") or {}).get("responses", {}).get("200") or {}).get("content")
        )
        for method in ("get", "post"):
            if method not in item:
                continue
            operations.append(
                operation(path, method.upper(), item[method], "get" in item, read_schema, counters)
            )
    return {"version": spec["info"]["version"], "operations": operations}


def operation(
    path: str, method: str, op: dict, has_read: bool, read_schema: dict | None, counters: dict
) -> dict:
    """Запись карты об одной операции."""
    section = section_of(path)
    counters[section] = counters.get(section, 0) + 1
    kind, reason = kind_of(method, path, has_read)
    query, missing = query_values(op.get("parameters") or [])
    answer = (op.get("responses", {}).get("200") or {}).get("content") or {}
    schema = json_schema(answer)
    entry = {
        "id": f"API-{section}-{counters[section]}",
        "method": method,
        "path": path,
        "title": op.get("summary") or path,
        "kind": kind,
    }
    optional = {
        "reason": reason,
        "query": query,
        "missing": missing,
        "schema": plain_schema(schema) if schema and kind == READ else None,
        "binary": bool(answer) and "application/json" not in answer,
        "sensitive": path.startswith(SENSITIVE_PREFIXES),
        "live": method == "GET" and path in LIVE_READS,
    }
    entry |= {key: value for key, value in optional.items() if value}
    body = json_schema((op.get("requestBody") or {}).get("content"))
    # Разрушающая операция с телом (какую папку удалить) без значения не отправляется вовсе
    if kind in (READ, DESTRUCTIVE) and body:
        if path in READ_BODIES:
            entry["body"] = READ_BODIES[path]
        else:
            entry["missing"] = [*missing, "тело запроса"]
    if kind == WRITE:
        entry |= write_field(body, read_schema)
        sample = json.dumps({entry["setting"]: 1})
        check_web_request(method, path, sample)
    return entry


def main() -> None:
    """Записать карту; путь к описанию можно передать первым аргументом."""
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    result = build(source)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8"
    )
    kinds: dict[str, int] = {}
    for entry in result["operations"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print(f"{result['version']}: {len(result['operations'])} операций, {kinds}")


if __name__ == "__main__":
    main()
