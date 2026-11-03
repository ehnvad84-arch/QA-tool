# Ревью: skeleton

Ревьюер: независимый ревьюер (Claude Opus 5.5) · Контекст: свежий · Дифф: main...feature/skeleton (до f25467d, 98 файлов, ~6,8 тыс. строк, прочитан полностью, кроме `uv.lock`) · Дата: 2026-11-03

**Вердикт:** approve. Новых blocker нет. Мерж всё равно ждёт T20 (зелёная сборка CI на Windows), и до него стоит закрыть W5, W6, W7 и W10: они воспроизводятся уже сейчас. W1–W4 и W9 сейчас не срабатывают, но их нужно закрыть до начала этапов 2, 3 и 5 (ONVIF через onvif-tt, адаптеры API, веб-обход). Иначе табу AD-7 держится только на документе.

Уровни: blocker / warning (= major) / nit (= minor). Все находки «новые»: на `main` были только документы. Старого долга нет.

## 1. Дифф: что изменилось и почему

- T1–T19 по плану: пакет `qa_tool` (core: модели, конфигурация, секреты, реестр проверок, SQLite, планировщик и рабочие процессы; драйвер ONVIF/SOAP, WS-Discovery; проверка CORE-CONNECT-1; протоколы HTML/PDF/Excel; окно с пятью экранами, подсказками и F1), mock-камера, тесты, сборка AppImage.
- T20 (CI и установщик Windows): файлы есть (`.github/workflows/ci.yml`, `packaging/windows/*`), но зелёной сборки ещё не было. Это известно, статус [!].
- Изменения вне плана, все записаны в progress.md:
  - `core/taboo.py` и правка AD-3/AD-7, never.md, security.md — по указанию директора посреди T18;
  - AD-4 переписан под «любые IP-камеры», добавлен `check_web_request` — по указанию директора;
  - свой WS-Discovery вместо пакета WSDiscovery (вместо него зависимость `defusedxml`);
  - поле `Stand.interface`;
  - команда `qa-tool doctor`;
  - два exe на Windows (окно и консоль);
  - `setup_console` (UTF-8 в консоли Windows).
- Вне плана и не записано: в репозиторий закоммичен `qa-tests.log`, вывод pytest (N13).

## 2. Соответствие спеке

| Сценарий | Реализован | Тест | Замечание |
|---|---|---|---|
| S1 Поиск | да | tests/drivers/test_onvif_generic.py::test_discovery_finds_all_mock_cameras_within_10s, tests/gui/test_cameras_stand.py::test_find_cameras_fills_table | GUI-тест проверяет только порты. Колонки «производитель», «модель», «на стенде» не проверяются. Чужой пакет валит поиск (W7) |
| S2 Добавление вручную и пароль | да | tests/gui/test_cameras_stand.py::test_eight_cameras_placed_ninth_rejected, tests/core/test_store.py::test_no_password_column_in_cameras, tests/core/test_config.py::test_password_in_stand_yaml_is_config_error | В базе проверяются только имена колонок, значение пароля по всей базе не ищется |
| S3 Места и место стенда | да | test_eight_cameras_placed_ninth_rejected, test_place_chosen_on_stand_screen_is_saved, tests/core/test_config.py::test_ninth_camera_is_rejected | — |
| S4 Параллельный прогон и «Остановить» | да | tests/gui/test_scenario_run.py::test_run_eight_cameras_from_window, ::test_stop_releases_protocols_for_what_was_done, tests/core/test_scheduler.py::test_stop_skips_unstarted_checks | Не покрыто: «Остановить» при зависшей начатой проверке (прерывание по тайм-ауту) |
| S5 Камера пропала | частично | tests/core/test_scheduler.py::test_lost_camera_marked_stand_error_others_finish | Проверка, которая шла в момент пропажи, получает «не прошла» и попадает в задачи (W8). Выпуск протокола камеры 3 тестом не проверяется. Тест нестабилен (W10) |
| S6 Фазы и ресурсы | да | test_eight_cameras_phases_locks_and_barrier | Покрыта одна синхронная проверка. При нескольких синхронных проверках на одном ресурсе возможна взаимная блокировка (W9) |
| S7 Протокол камеры | частично | tests/report/test_html.py::test_only_failed_checks_become_tasks, ::test_camera_answers_escaped_and_secrets_removed | Код есть. Тестом не проверены порог с источником, дата, серийный номер и прошивка в шапке |
| S8 Excel и сводный | да | tests/report/test_html.py::test_pdf_and_excel_published | Ответы камеры попадают в ячейки как формулы (W5) |
| S9 Подсказка 1 с | да | tests/gui/test_window.py::test_tooltip_delay_is_one_second, ::test_every_control_on_every_screen_has_tooltip_and_help, ::test_control_without_tooltip_is_caught | — |
| S10 F1 | да | tests/gui/test_help.py (3 теста) | — |
| S11 Прогон без окна | частично | tests/test_cli_run.py (4 теста) | Коды 0, 1 и 2 (только конфигурация) покрыты. Любая другая ошибка даёт код 1 вместо 2 (W6). Код 2 при «ошибке стенда» во время прогона не тестируется |
| S12 Установка | частично | ручная (T19 — AppImage на этой машине) | Windows и Ubuntu 24.04 не проверены, ждём T20 |
| S13 Секреты | да | tests/core/test_secrets.py, tests/drivers/test_onvif_generic.py::test_exchange_recorded_without_password_digest | Набор шаблонов узкий (N3) |
| S14 Разрушающие выключены | да | tests/core/test_checks.py::test_destructive_check_skipped_and_not_called | — |

### Непокрытые сценарии и случаи — для test-writer

1. S5: камера пропадает во время проверки, которая к ней обращается. Ожидается «ошибка стенда: нет связи», задачи нет. Сейчас будет «не прошла» (W8).
2. S5: после прогона с потерянной камерой 3 есть `protocol_3.{html,pdf,xlsx}`, итог — «Испытания не завершены».
3. S4: «Остановить», когда начатая проверка висит. Она закрывается по тайм-ауту, прогон получает состояние «остановлен».
4. S6: две и более синхронных проверки на общем ресурсе, одна из них не для всех камер (по производителю). Прогон должен завершиться без «Остановить» (W9).
5. S7: в протоколе есть порог, единица и источник, дата, серийный номер и прошивка из паспорта.
6. S11: ошибка не из конфигурации (папка прогонов недоступна на запись, нет хранилища паролей) даёт код 2 и понятный текст, а не трассировку (W6).
7. S1: ответ WS-Discovery с портом 99999 или с DTD/ENTITY пропускается, остальные камеры находятся (W7).
8. S8: модель или прошивка вида `=HYPERLINK(...)` в Excel остаётся текстом (W5).
9. Табу: GET-запросы Dahua `configManager.cgi?action=setConfig&Network...` и Axis `pwdgrp.cgi?action=update`, PUT Hikvision `/ISAPI/System/configurationData` блокируются. Плюс обходы `check_http`: полный URL, `//`, `./`, `%6E` (W1, W2).
10. Табу ONVIF: `SetHashingAlgorithm`, `SetPasswordComplexityConfiguration`, операции с сертификатами и TLS; тело с комментарием перед операцией (W3).

## 3. Безопасность

Вердикт security-check.md: fail (blocker нет; провалены A05, A06 и A10, все на уровне warning). Snyk не установлен и не запускался. agentshield не нужен: `.claude/`, хуки, MCP и скиллы в диффе не менялись.

### Отдельные проверки по запросу

- **Все пути отправки запросов камере идут через taboo.py:** нет.
  - Через табу идёт только `SoapClient.call` (`drivers/camera/soap.py:69`).
  - `checks/core/connect.py:12-28` (TCP и RTSP OPTIONS) и `core/worker.py:41` (проверка связи) открывают сокет напрямую. Это чтение, нарушения табу нет, но правило AGENTS.md «любой запрос к камере идёт через taboo.py» буквально не выполнено.
  - Ничто в коде не мешает проверке или onvif-tt (свой транспорт zeep) пойти мимо (W4).
  - `check_http` и `check_web_request` пока нигде не вызываются, а в нынешнем виде пропускают запросы, которые меняют сеть и пароли (W1–W3).
- **Секреты в протоколах, базе и stand.yaml:**
  - stand.yaml: пароля нет (`save_stand`); поле `password` у камеры отклоняется.
  - База: колонки пароля нет, причины и значения проходят `redact`.
  - Протоколы: `redact` перед выводом.
  - Пароль уходит в рабочий процесс через pickle по каналу multiprocessing и на диск не пишется.
  - Мелкие дыры: текст исключения и вывод в консоль не очищаются известным паролем; шаблоны не ловят `access_token=`, `pwd=`, `user:pass@` (N3).
- **Экранирование ответов камер:**
  - HTML: Jinja2 autoescape, тест с `<script>`.
  - PDF строится из уже экранированного HTML.
  - GUI: `QTableWidgetItem`, обычный текст.
  - XML разбирается через defusedxml.
  - Excel не экранируется (W5).
  - Имена файлов доказательств очищаются (`safe_name`), без учёта зарезервированных имён Windows (N4).
- **Планировщик:**
  - Гонки: блокировки «всё или ничего», рабочие процессы ждут снятия после «lost» (исправление e8bfbbe).
  - Взаимная блокировка барьеров — W9.
  - Потеря камеры во время проверки — W8.
  - Остановка: неначатые проверки получают «пропущена», начатые дорабатывают. «quit» во время ожидания блокировки принимается за разрешение (N7, только при сбое координатора).
  - Тест потери камеры нестабилен (W10).
- **T20:** известно, что CI ни разу не был зелёным. Нестабильный тест W10 и тихий пропуск Snyk (W11) прямо мешают закрыть T20.

## 4. Тесты и edge cases

- Полный прогон: `LD_LIBRARY_PATH=$HOME/.cache/qa-tool-syslibs/root/usr/lib/x86_64-linux-gnu ~/bin/qa-limited uv run pytest -q -p no:cacheprovider` → **1 failed, 122 passed за 173,7 с**.
  - Упал `tests/core/test_scheduler.py::test_lost_camera_marked_stand_error_others_finish`: у места 1 все три проверки получили «ошибка стенда» (`AssertionError: 1`).
  - Отдельный сценарий из scratch-скрипта, два прогона по 8 камер, ошибку не воспроизвёл. Тест нестабилен, причина не установлена (W10).
- `uv run ruff check .` и `ruff format --check .` чисты.
- Что я проверил скриптом на текущем коде (`taboo.py`, openpyxl, `parse_probe_matches`):
  - Пропущены без блокировки:
    - Dahua GET `setConfig&Network.eth0.IPAddress`;
    - Axis GET `pwdgrp.cgi?action=update`;
    - Hikvision PUT `/ISAPI/System/configurationData` и `/ISAPI/System/activate`;
    - `check_http("POST", "http://cam/api/network/...")`, а также `/api//network`, `/api/./network`, `/api/%6Eetwork`;
    - `check_onvif("<!-- <tds:GetUsers/> --><tds:SetUser/>")` и полный конверт с `SetNTP`;
    - `SetHashingAlgorithm`, `SetPasswordComplexityConfiguration`, `LoadCertificates`.
  - Заблокирован лишний запрос: PUT `/ISAPI/Image/channels/1/IrcutFilter` («lte» внутри «filter»).
  - openpyxl записал строку `=HYPERLINK(...)` с `data_type = 'f'`, то есть как формулу.
  - `parse_probe_matches` с портом 99999 бросает ValueError, с DTD — `EntitiesForbidden` (это не ParseError).
- Не покрыто тестами: список выше (п. 1–10). Кроме того: битый `run.sqlite` в папке прогонов (N10), YAML-файл не со словарём на верхнем уровне (N11), две камеры с одним IP и разными портами (N2).

## Находки

| # | Уровень | Происхождение | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|---|
| W1 | warning | новое | src/qa_tool/core/taboo.py:161, 170-179 | `check_web_request` не смотрит GET, HEAD и OPTIONS. Но Dahua CGI (`configManager.cgi?action=setConfig&Network…`), Axis VAPIX (`param.cgi?action=update&Network…`, `pwdgrp.cgi?action=update`) и многие OEM меняют сеть и пароли именно через GET. Список слов не ловит импорт конфигурации Hikvision (`PUT /ISAPI/System/configurationData`) и `/ISAPI/System/activate`. Для камеры без адаптера это единственная защита (AD-7): веб-обход этапа 5 нажмёт кнопку и выбьет камеру из сети. Тест с Dahua использует PUT, хотя настоящий Dahua шлёт GET, поэтому запись в журнале «Dahua/VAPIX блокируются» неверна. Сейчас функция нигде не вызывается | Для веб-обхода проверять адрес и строку запроса при любом методе. GET без слов-признаков пропускать, GET с `action=set*/update/add/delete` плюс признак — блокировать. Добавить признаки `configurationdata`, `activate`, `setconfig`. Тесты с настоящими GET-запросами Dahua и Axis |
| W2 | warning | новое | src/qa_tool/core/taboo.py:76-85 | `check_http` нормализует путь только через `strip("/")` и `lower()`. Полный URL, `//`, `./` и `%XX` проходят мимо запрещённых префиксов. Адаптер «Рувер» на этапе 3 может отправить `POST /api//network/...` или передать полный URL | `urlsplit` → `unquote` → `posixpath.normpath` → `lower`; принимать URL или путь; тесты на обходы |
| W3 | warning | новое | src/qa_tool/core/taboo.py:16-46, 59-73 | В списке ONVIF нет операций, которые косвенно ломают вход: `SetHashingAlgorithm`, `SetPasswordComplexityConfiguration`, `SetPasswordHistoryConfiguration`, `SetAuthFailureWarningConfiguration`, сертификаты и TLS (`LoadCertificates`, `DeleteCertificates`, `SetCertificatesStatus`, `SetClientCertificateMode`, операции Advanced Security `tas:*`). Операция определяется регуляркой по первому тегу: комментарий с Get* перед Set* или полный конверт в `body` проходят | Разбирать тело через defusedxml и проверять каждый дочерний элемент `Body`. Дополнить список. Надёжнее разрешительный список: Get* и явно разрешённые Set*, остальное — табу |
| W4 | warning | новое | src/qa_tool/checks/core/connect.py:12-28, src/qa_tool/core/worker.py:41; архитектура | Единой точки нет. Табу проверяет только `SoapClient`, а любая проверка может импортировать `httpx` или `socket`. onvif-tt на этапе 2 ходит через zeep мимо `SoapClient`. Утверждение AD-7 «onvif-tt или будущий модуль не смогут его обойти» в коде не обеспечено | Архитектурный тест: `httpx`, `socket`, `zeep`, `playwright` импортируются только в `drivers/`, каждый клиент вызывает `taboo.*`. Для zeep — Plugin или Transport с `check_onvif`. Чтение по TCP и RTSP явно записать в AD-7 как исключение или увести в драйвер |
| W5 | warning | новое | src/qa_tool/report/xlsx.py:54 | Formula injection: модель, прошивка и серийный номер от камеры (а также причина с текстом SOAP Fault) пишутся в ячейки как есть. Строка с `=` становится формулой (проверено: `data_type 'f'`). Камера с прошивкой `=HYPERLINK("http://…","Открыть")` или `=WEBSERVICE(…)` подменяет содержимое протокола ОТК и может слать данные наружу при открытии в Excel. Правило «ответы камер — недоверенные данные» нарушено для Excel | В `_table` для строк, начинающихся с `= + - @ \t \r`, выставлять `cell.data_type = "s"` или добавлять префикс `'`. Тест |
| W6 | warning | новое | src/qa_tool/gui/screens/run.py:155, src/qa_tool/cli.py:65, src/qa_tool/core/config.py:25,61 | Ловится только `ConfigError`. Окно: OSError (папка прогонов недоступна), `sqlite3.Error`, `keyring.errors.*` (нет Secret Service на Linux) убивают поток прогона, `state.running` остаётся True, «Начать» недоступна до перезапуска. CLI: трассировка и код 1, то есть «есть не прошла» вместо 2 (нарушение S11, ночной прогон отчитается неверно). YAML не со словарём даёт AttributeError, а не ConfigError | В `work()` ловить `Exception` и отдавать `failed`. В `cmd_run` превращать любую неожиданную ошибку в код 2 с текстом. В `_read_yaml` проверять, что на верхнем уровне словарь |
| W7 | warning | новое | src/qa_tool/drivers/camera/discovery.py:63, 99; src/qa_tool/gui/screens/cameras.py:121 | Одно устройство в подсети с XAddr `…:99999/…` (ValueError) или с DTD/ENTITY в ответе (`EntitiesForbidden` — ValueError, не ParseError) роняет `discover`. CLI печатает трассировку, в окне кнопка «Найти камеры» остаётся выключенной навсегда (поток ловит только OSError). S1 ломается | Ловить `(ElementTree.ParseError, ValueError)` на каждый пакет и на каждый ProbeMatch. В потоке GUI — `Exception` |
| W8 | warning | новое | src/qa_tool/checks/core/connect.py:60-61, src/qa_tool/core/worker.py:105-113 | Камера пропала во время проверки: `CameraUnreachable` превращается в FAILED, результат записывается как «не прошла» и уходит в задачи программистам. S5 требует «ошибка стенда: нет связи», соглашение — «ошибка стенда не становится задачей». Тест обходит этот случай: фиктивная проверка камеру не трогает | В рабочем процессе после результата не «прошла» повторять `camera_alive`; если камеры нет — заменить результат на `stand_error("нет связи")` и отправить `lost`. Тест (п. 1 списка) |
| W9 | warning | новое | src/qa_tool/core/scheduler.py:157-161, 213-227 | Барьер синхронной проверки перепроверяется только на сообщениях barrier, phase_done и lost, но не при освобождении ресурса синхронной проверки. Пример: синхронные X, Y, Z на ресурсе «свет», Y не для камеры B (по производителю). A ждёт «go» по Y (свет ещё держит X у B). B заканчивает X, свет освобождается, но Y не перепроверяется; B уходит на барьер Z и ждёт A. Прогон висит без тайм-аута до «Остановить», в CLI — навсегда. Сейчас таких проверок нет | После `_release_resources(f"sync:…")` вызывать `_try_barrier` по всем ожидающим барьерам. Добавить тайм-аут ожидания барьера и блокировки. Тест (п. 4) |
| W10 | warning | новое | tests/core/test_scheduler.py:103-130 | Тест нестабилен: в полном прогоне упал (у места 1 все проверки — «ошибка стенда»), в двух отдельных прогонах прошёл. Причину не видно, сообщение проверки печатает только номер места. Это третий нестабильный тест планировщика (до этого 695c6bf, e8bfbbe), прямой риск красного CI в T20 | Выводить `reason` в сообщении проверки. Найти причину: похоже на гибель или медленный старт рабочего процесса в песочнице. Если это гонка в координаторе — чинить код, не тест |
| W11 | warning | новое | .github/workflows/ci.yml:38-41 | Без `SNYK_TOKEN` задание Snyk тихо завершается кодом 0, а security.md п. 5 требует Snyk на каждом PR до мержа. Локально Snyk не установлен, по этой ветке он не запускался ни разу | На PR в `main` без токена задание должно падать (или Гейт 3 требует отчёт Snyk). Прогнать `snyk test` по `uv export` до мержа |
| N1 | nit | новое | src/qa_tool/core/taboo.py:109, 120, 136 | Подстроки дают лишние блокировки: «lte» ловит «filter» (ИК-фильтр, шумоподавление), «port» — «report», «support», «export», «auth» — «author». Веб-проверки дня и ночи и картинки потеряют покрытие | Сравнивать по токенам или с границами слов (`[^a-z]lte[^a-z]`), список запретов оставить широким |
| N2 | nit | новое | src/qa_tool/core/secrets.py:19, 24 | Ключ хранилища — только адрес. Две камеры за одним IP (NAT, разные порты) делят один пароль, и ввод второго затирает первый | Ключ `адрес:порт` (с переходом со старого ключа) |
| N3 | nit | новое | src/qa_tool/core/secrets.py:30-48; src/qa_tool/core/checks.py:166; src/qa_tool/cli.py:59-60 | Шаблоны не ловят `access_token=` (у `\b` после `_` нет границы), `sessionid`, `pwd=`, `auth=` и `user:pass@` в URL. Текст исключения и вывод `event.detail` в консоль не очищаются известным паролем | Расширить шаблоны. В `run_check` вызывать `redact(..., [ctx.password])` для `reason`, `actual` и `expected` |
| N4 | nit | новое | src/qa_tool/core/store.py:32-36 | `safe_name` пропускает зарезервированные имена Windows (`CON`, `NUL`, `COM1`). Одинаковые после очистки имена перезаписывают друг друга | Префикс для зарезервированных имён, суффикс при совпадении |
| N5 | nit | новое | src/qa_tool/drivers/camera/discovery.py:62, 82 | Адрес камеры берётся из XAddrs, а не из адреса отправителя: устройство может «назваться» чужим адресом. TTL 2 выпускает Probe за пределы своей подсети (security.md: «только в своей подсети») | TTL 1. Помечать находку, если хост XAddrs не совпадает с отправителем |
| N6 | nit | новое | src/qa_tool/drivers/camera/soap.py:60, 75-86 | Логин вставляется в XML без экранирования (`&` или `<` ломают запрос). Размер ответа камеры не ограничен | `xml.sax.saxutils.escape(username)`; ограничить размер ответа |
| N7 | nit | новое | src/qa_tool/core/worker.py:95, 100 | Если во время ожидания «grant» или «go» придёт «quit» (только при сбое координатора), он считается разрешением: проверка запустится без блокировки ресурса | Ждать именно `grant`/`go`; на `quit` — выход |
| N8 | nit | новое | src/qa_tool/core/worker.py:109-111 | «Нет связи с начала» определяется по подстроке «нет связи» в тексте причины. Хрупко: правка текста сломает логику | Признак в `result.data` (например, `unreachable: True`) |
| N9 | nit | новое | tests/test_cli_run.py:38-40; src/qa_tool/checks/core/connect.py:60 | Неверный пароль на стенде даёт «не прошла» и задачу программистам, хотя это ошибка настройки стенда. Нужна проверка: решение человека, считать ли `NotAuthorized` «ошибкой стенда» | Вынести вопрос директору; если да — `stand_error` для `NotAuthorized` у CORE-CONNECT-1 |
| N10 | nit | новое | src/qa_tool/gui/screens/protocols.py:27 | Один битый `run.sqlite` ломает весь экран «Протоколы». Открытие старого прогона выполняет `CREATE TABLE` (запись) | `try` на каждый прогон; открывать базу только для чтения (`mode=ro`) |
| N11 | nit | новое | src/qa_tool/core/config.py:10, 34 | `password:` на верхнем уровне stand.yaml или под другим именем (`pwd`) не отклоняется и остаётся в файле | Проверять ключи верхнего уровня; список `pass`, `pwd`, `secret` |
| N12 | nit | новое | .github/workflows/ci.yml | Нет `permissions: contents: read`; actions закреплены тегами, а не SHA; `uv sync` без `--locked`; `snyk@1` плавающий | Добавить `permissions`, `--locked`, закрепить версии |
| N13 | nit | новое | qa-tests.log | В репозитории вывод pytest, к делу не относится | Удалить, добавить `*.log` в `.gitignore` |
| N14 | nit | новое | src/qa_tool/gui/screens/cameras.py:181-184 | Если `set_password` упадёт (не ConfigError), камера уже добавлена в `stand` в памяти, а пароль не сохранён. Исключение уходит в слот Qt | Сначала сохранить пароль, потом `add_camera`; ловить `keyring.errors.KeyringError` |
| N15 | nit | новое | src/qa_tool/drivers/camera/onvif_generic.py:45-57 | Адреса из `GetCapabilities` могут указывать на чужие хосты. Когда этап 2 начнёт по ним ходить, это выход за «адреса из списка стенда» | При использовании XAddr сверять хост с `camera.address` |

## Старый долг

Нет: на `main` были только документы.
