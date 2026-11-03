# Security-check: <slug>

**Вердикт:** pass | fail <!-- fail, если ниже есть хотя бы один fail -->

Проверяются только фреймворки, отмеченные применимыми в docs/rules/security.md. Неприменимые разделы удали.
Каждый пункт: pass / fail / n/a и где именно.

## Базовый минимум

- [ ] В диффе и истории ветки нет секретов
- [ ] `.env` не в индексе; `.gitignore`, `.cursorignore` и deny в `.claude/settings.json` на месте
- [ ] Нет доступа к продакшну: прод-ключей, прод-адресов, команд деплоя
- [ ] У новых сервисов, токенов и агентов минимальные права
- [ ] Сканеры запущены (ниже)

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | |
| Snyk — код (SAST) | `snyk code test` | |
| Claude Code Security / `/security-review` | запускает главная сессия | |

## OWASP Top 10

<!-- По актуальной редакции: owasp.org/Top10. Одна строка на каждый пункт редакции. -->

| Пункт | Результат | Где |
|---|---|---|
| | | |

## OWASP LLM Top 10 (2025)

| Пункт | Результат | Где |
|---|---|---|
| LLM01 Prompt Injection | | |
| LLM02 Sensitive Information Disclosure | | |
| LLM03 Supply Chain | | |
| LLM04 Data and Model Poisoning | | |
| LLM05 Improper Output Handling | | |
| LLM06 Excessive Agency | | |
| LLM07 System Prompt Leakage | | |
| LLM08 Vector and Embedding Weaknesses | | |
| LLM09 Misinformation | | |
| LLM10 Unbounded Consumption | | |

## OWASP Agentic AI Threats

| Пункт | Результат | Где |
|---|---|---|
| T1 Memory Poisoning | | |
| T2 Tool Misuse | | |
| T3 Privilege Compromise | | |
| T4 Resource Overload | | |
| T5 Cascading Hallucination Attacks | | |
| T6 Intent Breaking & Goal Manipulation | | |
| T7 Misaligned & Deceptive Behaviors | | |
| T8 Repudiation & Untraceability | | |
| T9 Identity Spoofing & Impersonation | | |
| T10 Overwhelming Human in the Loop | | |
| T11 Unexpected RCE and Code Attacks | | |
| T12 Agent Communication Poisoning | | |
| T13 Rogue Agents in Multi-Agent Systems | | |
| T14 Human Attacks on Multi-Agent Systems | | |
| T15 Human Manipulation | | |

## Находки

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| 1 | blocker / major / minor | <файл:строка> | | |
