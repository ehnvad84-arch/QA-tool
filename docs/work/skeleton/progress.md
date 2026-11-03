# Журнал: skeleton

<!-- Здесь факты, а не планы. Статусы задач — только в plan.md. -->

## Handoff

<!-- Перезаписывается в конце каждой сессии и итерации. Здесь всё, что нужно следующей сессии с чистым контекстом. -->

- Этап: 1 — планирование, Гейт 1 (идёт plan-reviewer, затем утверждение директором)
- Последнее сделанное: спека S1–S14 и план T1–T20 (коммит f438d58)
- Следующий шаг: утверждение плана директором → T1
- Важно знать: репозиторий ehnvad84-arch/QA-tool ещё не создан — `gh repo create` блокирует автоклассификатор, директор запускает команду сам; разделы «UI» и «Архитектура/Безопасность» написаны главной сессией (не ui-designer/backend-architect)

## Журнал

<!-- Только добавлять, сверху вниз: дата · задача · что сделано · коммит · отклонения от плана или спеки. -->

- 2026-11-03 · этап 1 · спека и план skeleton · f438d58 · разделы UI и архитектуры — главная сессия вместо субагентов (весь контекст уже собран в сессии)
- 2026-11-03 · Гейт 1 · plan-reviewer: approve, 10 warning — все закрыты правкой спеки (scenario.yaml, «Остановить», ffmpeg BtbN LGPL) и плана (критерии T7/T10/T11 через run.sqlite, summary.*, экран «Стенд» в T15, .env.example, Snyk, keyring-тестовый бэкенд, экранирование, команда mock для ручных задач, T19 — только Linux, .exe и проверка на Windows — в T20)
- 2026-11-03 · T1 · каркас пакета, uv + Python 3.12, `qa-tool --version` · проверка: `uv run qa-tool --version` → qa-tool 0.1.0; ruff check/format чисто; pytest — 1 passed · отклонение: pytest-qt добавится в T14 (на машине нет libEGL.so.1 — нужен `sudo apt install libegl1`); .workflow исключён из ruff
- 2026-11-03 · T2 · модели Stand/Camera/Scenario/Threshold, чтение stand/scenario/thresholds.yaml · проверка: pytest — 11 passed (9-я камера, пароль в yaml, умолчания сценария, источник порога); ruff чисто
- 2026-11-03 · T3 · keyring + QA_TOOL_PASSWORD_<n>, redact() для HTTP/SOAP/JSON/query, .env.example · проверка: pytest — 21 passed (тестовый бэкенд keyring в памяти); ruff чисто
- 2026-11-03 · T4 · Registry/CheckSpec/CheckResult, фазы, план проверок по производителю, месту и галочкам · проверка: pytest — 31 passed (разрушающая без галочки не вызывается; «прошла» без доказательства → ошибка стенда); ruff чисто
- 2026-11-03 · T5 · tests/mock_camera.py: SOAP GetDeviceInformation/GetCapabilities с PasswordDigest, RTSP OPTIONS, общий ответчик WS-Discovery, `python -m tests.mock_camera --count 9 --write-stand`; Camera.rtsp_port · проверка: pytest — 34 passed (8 паспортов, неверный пароль → fault, stop закрывает порты); ruff чисто
- 2026-11-03 · T6 · SoapClient (UsernameToken digest, defusedxml, обмен с вырезанными секретами), OnvifCamera (паспорт, возможности), discover() multicast/unicast · проверка: pytest — 40 passed (поиск 8 mock ≤ 10 с по multicast через lo и по unicast; неверный пароль → OnvifFault; недоступна → CameraUnreachable); ruff чисто · отклонения: свой WS-Discovery вместо пакета WSDiscovery (зависимость заменена на defusedxml); в Stand добавлено поле interface — IP сетевой карты к камерам (multicast без явной карты на машине разработки не ходит); в сети машины разработки отвечает ещё одно ONVIF-устройство (qemu-arm-static, порт 18080) — тест проверяет вхождение, а не равенство
- 2026-11-03 · T7 · CORE-CONNECT-1 (HTTP, RTSP OPTIONS, GetDeviceInformation → data.passport, обмен как доказательство), общий REGISTRY и qa_tool.checks.load_all() · проверка: pytest — 43 passed (живая → прошла с паспортом; выключенная → не прошла «нет связи по HTTP»; неверный пароль → NotAuthorized); ruff чисто
- 2026-11-03 · T8 · RunStore: run.sqlite (runs, cameras без пароля, results с порогом и источником), evidence/ с очисткой имён и секретов, номер QA-ГГГГММДД-ННН · проверка: pytest — 47 passed; ruff чисто
- 2026-11-03 · T9 · core/worker.py (процесс камеры) + core/scheduler.py (Runner: фазы, блокировки всё-или-ничего, барьер синхронной фазы с захватом ресурсов на группу, «Остановить», тайм-аут проверки), CheckSpec.order/timeout_s · проверка: pytest — 49 passed; test_scheduler: 8 камер — свет не пересекается, синхронная стартует в пределах 1 с, фазы не смешаны; остановка → «пропущена: прогон остановлен», состояние «остановлен»; ruff чисто
- 2026-11-03 · T10 · проверка связи перед каждой проверкой (после первого ответа камеры), lost → «ошибка стенда: нет связи», зависание → «проверка зависла (> N с)» и снятие только этой камеры · проверка: pytest tests/core/test_scheduler.py — 4 passed (камера 3 гасится посреди прогона: 7 камер прошли, у места 3 остальные — «ошибка стенда / нет связи»); ruff чисто
- 2026-11-03 · T11 · cli: run (--stand/--scenario/--thresholds, сценарий и пороги рядом со stand.yaml по умолчанию), discover, коды 0/1/2; пароли разрешаются до создания папки прогона · проверка: pytest — 55 passed (8 mock → 0 и run.sqlite; неверный пароль у места 4 → 1; нет паролей → 2 без папки прогона; discover находит MOCK-1); ruff чисто
