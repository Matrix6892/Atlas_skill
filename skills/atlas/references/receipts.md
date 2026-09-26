# Квитанции: единственный способ записи

Записывает только `atlas.py write`. Квитанцию JSON кладут в `.atlas/.local/inbox/<имя>.json` (инструментом Write) и передают через `--file`; после записи файл удаляется. Можно подать и на stdin, но heredoc с JSON Claude Code блокирует. Писатель проверяет каждую операцию, выдаёт ID и номер снимка, сохраняет транзакцию атомарно, перечитывает её и только после этого пишет «Записано, запись подтверждена».

```json
{
  "key": "2026-09-24-login-r7",
  "base_snapshot": 41,
  "all_or_nothing": false,
  "ops": [ {"op": "…", "ref": "t", "group": "g1", "expect": {"th-…": 3}, "source": {"quote": "…"}} ]
}
```

- `key` — ключ повторной доставки. Повтор с тем же содержимым вернёт прежнее подтверждение, а другое содержимое под тем же ключом считается конфликтом. Ставь ключ, если квитанцию могут повторить.
- `base_snapshot` — номер снимка из сводки. Если объект команды или его зависимость (решение, договорённость по теме) изменила **другая** сессия после этого снимка, команда получит конфликт.
- `all_or_nothing: true` — режим владельца «применить пакет только целиком».
- `ref` — короткое имя, чтобы сослаться на созданный объект ниже в той же квитанции: `"thread": "@t"`.
- `group` — операции одной группы применяются по принципу «всё или ничего». Операции без группы независимы. Наблюдения (`note.*`, `attempt`, `park.add`, `park.classify`, `verify`, `decision.ack`, `decision.applied`, `capture.recovered`) сохраняются, даже если команда из их группы получила конфликт.
- `expect` — ожидаемые ревизии объектов, `{"ID": ревизия}`. Если ревизия другая, будет конфликт; ревизию показывает `atlas show ID`.
- `source` — **слова владельца**: `{"quote": "его фраза", "at": "время, если известно"}`. Писатель требует их везде, где нужно согласие (такие поля отмечены ★). Тип источника ставит ядро; повысить его полем нельзя.

Сослаться на объект можно так:
- тема — ID `th-…`, `@ref` или точное название;
- вопрос или решение — ID `dec-…`, `@ref`, номер (`4`, `"вопрос 4"`);
- условие готовности — ID `cr-…` или `@ref`;
- запись парковки — ID `park-…`, `@ref` или точный текст;
- результат — ID `res-…` или `@ref`, либо `"thread": …` (последняя версия) с `"version": 7`.

Коды выхода: 0 — всё записано; 3 — записано частично; 4 — ничего не записано; 5 — ошибка протокола или повреждение; 2 — ошибка вызова.

## Проект

| op | Поля | Заметки |
| --- | --- | --- |
| `goal.set` | `text`, `scenario`, `reason`, ★`source` | Смена цели создаёт новую версию; прежняя цель остаётся в истории и достигнутой не считается |
| `project.rename` | `name`, ★`source` | |
| `settings.set` | `key`: `wip_limit` \| `stale_parking_days`, `value` (целое), ★`source` | Лимит тем в работе по умолчанию 3 |

## Темы и условия готовности

| op | Поля | Заметки |
| --- | --- | --- |
| `thread.create` | `title`, `why`, `state`: `candidate`\|`planned`\|`active`\|`paused` (по умолчанию `active`), `leads_to_goal`, `parent`, `reason` | Найденное разбором создаётся как `candidate`. Состояние `active` проверяет лимит тем |
| `thread.state` | `thread`, `state`: `planned`\|`active`\|`paused`\|`frozen`\|`released`, `reason`, `source`, `over_limit` | ★ Слова владельца нужны для `released`, `frozen`, для выхода из `candidate`/`released`/`frozen` и для превышения лимита. Для `released` и для возврата темы обязательна причина (`reason`) |
| `thread.update` | `thread`, `title`, `why`, `leads_to_goal` | Косметика: дата продвижения не меняется |
| `criterion.add` | `thread`, `text`, `observable` (по умолчанию true), `how` | Текст — одна проверяемая фраза о том, что должен уметь человек |
| `criterion.revise` | `criterion`, `text`, `substantive` (обязательно true/false) | При `true` прежние проверки к условию больше не относятся |
| `criterion.withdraw` | `criterion`, `reason`, ★`source` | |

Состояние «результат предъявлен» ядро **выводит само** из последнего результата. Напрямую его не записывают.

## Наблюдения о работе

| op | Поля |
| --- | --- |
| `note.progress` | `text`, `thread`, `recovered`: `git`\|`transcript`\|`chat` (если восстановлено задним числом) |
| `note.learned` | `text`, `thread`, `recovered` |
| `note.debt` | `text`, `thread` (обязательно), `recovered` |
| `note.resolve` | `note` (ID костыля), `text` |
| `attempt` | `thread`, `approach`, `outcome`: `failed`\|`partial`\|`succeeded`, `new_approach` (по умолчанию true), `problem`, `learned`, `criterion`, `recovered` |

## Решения и «Нужно от вас»

| op | Поля | Заметки |
| --- | --- | --- |
| `decision.ask` | `question` (словами владельца, с вариантами: «Куда вести после входа: в профиль или на главную?»), `options` [{`label`, `consequence`}] (0 или 2–4), `advice` {`option`/`text`, `wrong_if` — обязательно}, `provisional` {`option`/`text`}, `urgency`: `normal`\|`blocking`\|`deadline`, `deadline`, `can_defer`, `risk`: `normal`\|`money`\|`publish`\|`real_data`\|`external`\|`irreversible`\|`unknown`, `thread`, `criterion` | Одно решение — одна карточка: повторный такой же вопрос отклоняется. Временный выбор запрещён, если `risk` ≠ `normal` |
| `decision.provisional` | `decision`, `option`/`text`, `until` | Временный выбор агента внутри той же карточки |
| `decision.decide` | новое решение: `text`, `reason`, `thread`, `risk`, ★`source`. Ответ на вопрос: `decision`, `option` и/или `text`, `reason`, ★`source` — или `delegated: true` после `decision.delegate` | Решение с тем же текстом, что уже записано, отклоняется: это отсылка к прошлому (правило 4). Пиши `text` смыслом, например «После входа ведём в профиль» |
| `decision.delegate` | `decision`, ★`source` («реши сам») | Не действует, если затронуты деньги, публикация, настоящие данные, внешние обязательства, необратимость или неизвестный риск |
| `decision.defer` | `decision`, `source` | «Позже»: вопрос остаётся в очереди |
| `decision.supersede` | `decision`, `text`, `reason`, ★`source` | «Решение больше не действует»: новое заменяет старое, старое остаётся в истории |
| `decision.withdraw` | `decision`, `reason`, `source` (★ для действующего решения) | |
| `decision.ack` | `decision` | Эта сессия получила решение. Понимания не доказывает |
| `decision.applied` | `decision`, `text` | Агент сообщает, что учёл решение в работе |

## Парковка

| op | Поля | Заметки |
| --- | --- | --- |
| `park.add` | `text`, `kind`: `idea`\|`question`, `context` («в сессии про вход»), `quote`, `goal_related` (мнение агента), `recovered` | Идея — ещё не тема |
| `park.classify` | `item`, `goal_related` | |
| `park.promote` | `item`, `to`: `planned`\|`active`, `title`, `why`, ★`source`, `over_limit` | Создаёт тему |
| `park.release` | `item`, `reason`, ★`source` | «Забрось»: запись уходит в архив идей, её можно вернуть |
| `park.keep` | `item` | «Оставь» |
| `park.restore` | `item`, ★`source` | |

Пакет уборки («первое и второе в спринт, третье забрось») разбей на **независимые** операции без общей группы. Тогда ошибка в одном пункте не заблокирует остальные.

## Результат, проверка, приёмка — три оси

| op | Поля | Заметки |
| --- | --- | --- |
| `result.present` | `thread`, `criteria` [условия в составе версии], `summary` (что сделано, словами владельца), `excluded` [что не входит], `fit_for`, `not_fit_for`, `caveats` [], `claim` (что сказал агент), `check` {`run`, `steps` (до 4), `expect`, `failure`, `duration` («около пяти минут, включая запуск»)} | Версия rN растёт сама. Ядро привязывает её к отпечатку содержимого файлов |
| `result.withdraw` | `result`/`thread`, `reason` | |
| `verify` | `result`/`thread`, `criteria` (по умолчанию все в версии), `method`: `owner_manual`\|`agent_report`\|`external`, `outcome`: `passed`\|`failed`\|`could_not_check`, `environment` («на компьютере»), `limitations` [], `detail` (обязательно для `external`), ★`source` для `owner_manual` | `automated_run` через `write` понижается до «со слов агента». Для «проверено тестом» есть только `atlas check`. «Не смог запустить» записывается как `could_not_check`, а не как провал |
| `accept` | `result`/`thread`, `criteria` (подмножество версии; по умолчанию все), `outcome`: `accepted`\|`accepted_with_caveat`\|`rejected`, `purpose` («показа»; обязателен, кроме отказа), `caveat`, `rejection` {`class`: `defect`\|`new_wish`\|`environment`\|`check_error`, `reason`}, `old_version_ok`, ★`source` | Приёмка не распространяется на соседние условия и на новую версию. Если уже есть более новая версия, нужен `old_version_ok` |

Фраза «Проверил, работает, но дизайн не принимаю» даёт **две** операции: `verify` (passed, owner_manual) и `accept` (rejected, `new_wish` или `defect`).

## Договорённость и правила

| op | Поля | Заметки |
| --- | --- | --- |
| `frame.agree` | `doing`, `stop_when` (оба обязательны), `where`, `not_touching`, `decide_myself`, `ask_you`, `budget`, `data_access`, `recovery`, `permissions` [{`what`, `limit`}], `thread`, `standing` («так всегда»), ★`source` («ок») | Новая договорённость по той же теме закрывает прежнюю |
| `frame.close` | `frame`, `reason` | |
| `rule.propose` | `text` | «Не спрашивай такое»: правило не действует, пока владелец не подтвердит |
| `rule.confirm` / `rule.revoke` | `rule`, ★`source` | |

## Закладка

| op | Поля |
| --- | --- |
| `bookmark.save` | `stopped_at` (обязательно: где остановились, одной строкой), `today` [], `learned` [], `failed` [], `started` [], `next` {`text`, `kind`: `check_result`\|`answer_decision`\|`continue_thread`\|`other`, `ref` (тема, вопрос или результат), `why` («это откроет показ другу»)}, `thread` |

После записи `write` сам выводит экран Э7. Его нужно показать владельцу. Шаг `next` при следующем входе проверяется заново и отбрасывается, если устарел (например, результат уже принят).

## Исправление (Э11)

| op | Поля |
| --- | --- |
| `correct` | `target` (ID объекта или записи, либо номер вопроса), `action`: `withdraw` (было ошибкой — скрыть) \| `edit` (поля `title`, `why`, `text`, `question`, `kind`, `stopped_at`) \| `link` (заменить ссылкой на существующий объект `link_to`), `was`, `becomes`, `source` |

История не стирается: исправление — это новая запись. Код оно не меняет.

## Восстановление после неполной записи

| op | Поля |
| --- | --- |
| `capture.recovered` | `text`, `from`: `git`\|`transcript`\|`chat`, `gap` (ID пробела из сводки), `session` |

Восстановленные факты записывай обычными `note.progress` и `attempt` с полем `recovered`. На экранах они будут помечены «восстановлено задним числом». Решения из старого чата записывай только после подтверждения владельцем.

## Пример: предъявить результат и проверить его

Файл `.atlas/.local/inbox/login-r7.json`:

```json
{"key": "login-r7", "ops": [
  {"op": "result.present", "ref": "r", "thread": "Вход через Google",
   "criteria": ["cr-aaaa", "cr-cccc"],
   "summary": "человек входит через Google и видит своё имя в профиле",
   "excluded": ["вход с телефона", "показ ошибок входа"],
   "fit_for": "показа другу с вашего компьютера", "not_fit_for": "настоящих пользователей",
   "caveats": ["только тестовые данные"], "claim": "вход работает",
   "check": {"run": "Скажите агенту «запусти проект» — он откроет страницу сам.",
             "steps": ["Нажмите «Войти через Google» и выберите свой аккаунт."],
             "expect": "страница профиля, вверху ваше имя",
             "failure": "белый экран или снова страница входа",
             "duration": "около пяти минут, включая запуск"}}
]}
```

```bash
atlas write --file .atlas/.local/inbox/login-r7.json
atlas check --thread "Вход через Google" --criteria cr-aaaa -- npm test
atlas result "Вход через Google"     # показать владельцу карточку Э4
```
