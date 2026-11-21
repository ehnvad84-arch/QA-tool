# Протокол испытаний IP-камеры № {{ p.protocol_id }}

Прогон {{ p.run_id }} · начат {{ p.started }} · окончен {{ p.finished }} · qa-tool {{ p.version }}

| Поле | Значение |
|---|---|
| Камера | {{ p.camera_name }} |
| Производитель | {{ p.passport.manufacturer }} |
| Модель | {{ p.passport.model }} |
| Прошивка | {{ p.passport.firmware }} |
| Серийный номер | {{ p.passport.serial }} |
| Аппаратная версия | {{ p.passport.hardware }} |
| Адрес · место на стенде | {{ p.address }} · место {{ p.slot }} |
| Место стенда | {{ p.place }} |
| Изменяющие проверки | {{ "разрешены" if p.scenario.allow_writes else "запрещены" }} |
| Разрушающие проверки | {{ "разрешены" if p.scenario.allow_destructive else "запрещены" }} |

## Заключение

**{{ p.verdict }}**

Проверок: {{ p.rows | length }} · прошла: {{ p.counts["прошла"] }} · не прошла: {{ p.counts["не прошла"] }} · ошибка стенда: {{ p.counts["ошибка стенда"] }} · пропущена: {{ p.counts["пропущена"] }}

{% if p.image and p.image.restore_status == "не прошла" %}
> **Картинка: настройки не вернулись — верните вручную.** {{ p.image.restore_reason }}

{% endif %}
{% if p.codec and p.codec.restore_status == "не прошла" %}
> **Кодеки: настройки не вернулись — верните вручную.** {{ p.codec.restore_reason }}

{% endif %}
{% if p.api and p.api.alarm %}
> **{{ p.api.alarm }}.** {{ p.api.alarm_how }}

{% endif %}
## 1. Сводка по модулям

| Модуль | Прошла | Не прошла | Ошибка стенда | Пропущена |
|---|---|---|---|---|
{% for module, c in p.modules %}
| {{ module }} | {{ c["прошла"] }} | {{ c["не прошла"] }} | {{ c["ошибка стенда"] }} | {{ c["пропущена"] }} |
{% endfor %}

{% if p.profiles.matrix %}
## Профили ONVIF

Объявлены камерой: {{ p.profiles.declared | join(", ") or "ни одного" }}.

| Профиль | Обязательная функция | Итог | Основание |
|---|---|---|---|
{% for v in p.profiles.matrix %}
| {{ v.profile }}{% if v.profile not in p.profiles.declared %} (не объявлен){% endif %} | {{ v.feature }} | {{ v.verdict }} | {{ v.basis }} |
{% endfor %}

{% endif %}
## 2. Результаты проверок

| № | Проверка | Что делали | Ожидание | Факт | Порог (источник) | Результат | Доказательство |
|---|---|---|---|---|---|---|---|
{% for r in p.rows %}
| {{ r.number }} | **{{ r.check_id }}** {{ r.title }} ({{ r.module }}) | {{ r.description }} | {{ r.expected }} | {{ r.actual }} | {{ r.threshold }} | {{ r.status }}{% if r.reason %}: {{ r.reason }}{% endif %} | {{ r.evidence }} |
{% endfor %}

## 3. Задачи программистам

{% for t in p.tasks %}
### {{ t.number }}. {{ t.title }}

- Модуль: {{ t.module }}
- Важность: {{ t.severity }}
- Шаги воспроизведения:
{% for step in t.steps %}
  {{ loop.index }}. {{ step }}
{% endfor %}
- Ожидалось: {{ t.expected }}
- Получено: {{ t.actual }}
- Доказательство: {{ t.evidence }}

{% else %}
Замечаний нет.

{% endfor %}
«Ошибка стенда» и «пропущена» в задачи не входят: это не дефект камеры.
{% if p.appendices %}

Приложения (снимки, графики, таблицы — в HTML, PDF и Excel этого протокола): {% for n, title in p.appendices %}{{ n }} — {{ title }}{{ "; " if not loop.last else "." }}{% endfor %}

{% endif %}
