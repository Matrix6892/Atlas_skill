"""Russian wording helpers: dates in words (§7а rule 10), owner vocabulary."""

import datetime as _dt

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAY_NOM = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAY_ACC = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
WEEKDAY_GEN = ["понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья"]

THREAD_STATE = {
    "candidate": "кандидат",
    "planned": "следующий спринт",
    "active": "в работе",
    "paused": "на паузе",
    "presented": "результат предъявлен",
    "frozen": "заморожено",
    "released": "отпущено",
}

AGENT_NAMES = {
    "claude-code": "Claude Code",
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "opencode",
}

# Words from §4.2 that get a one-time explanation on first appearance in a session.
TERM_HINTS = {
    "версия": "номер растёт при каждой переделке",
    "снимок": "страница из записей на этот момент, сама не обновляется",
    "условие готовности": "одна проверяемая фраза о том, что должен уметь человек",
    "хук": "событие, на которое агент вызывает Атлас",
    "захват": "сохранение записей по ходу работы агента",
}


def parse_ts(value):
    """Parse an ISO timestamp written by the core; returns aware local datetime."""
    if not value:
        return None
    try:
        ts = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return ts.astimezone()


def now():
    return _dt.datetime.now().astimezone().replace(microsecond=0)


def now_iso():
    return now().isoformat()


def day_month(ts):
    return "%d %s" % (ts.day, MONTHS_GEN[ts.month - 1])


def hm(ts):
    return ts.strftime("%H:%M")


def short_date(ts):
    """24.09 — only for tables and topic history (§7а rule 10)."""
    return ts.strftime("%d.%m")


def _relative_day(ts, today):
    delta = (today.date() - ts.date()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    return None


def words_full(ts, today=None, with_time=True, relative=True):
    """«четверг, 24 сентября, 22:10» / «сегодня, 25 сентября, 09:40»."""
    today = today or now()
    rel = _relative_day(ts, today) if relative else None
    head = rel if rel else WEEKDAY_NOM[ts.weekday()]
    text = "%s, %s" % (head, day_month(ts))
    if with_time:
        text += ", " + hm(ts)
    return text


def words_on(ts, today=None, with_time=False):
    """«в четверг, 24 сентября» — for "when it happened"."""
    today = today or now()
    rel = _relative_day(ts, today)
    head = rel if rel else WEEKDAY_ACC[ts.weekday()]
    text = "%s, %s" % (head, day_month(ts))
    if with_time:
        text += ", " + hm(ts)
    return text


def words_from(ts):
    """«от четверга, 24 сентября, 22:10» — for bookmarks."""
    return "от %s, %s, %s" % (WEEKDAY_GEN[ts.weekday()], day_month(ts), hm(ts))


def capital(text):
    return text[:1].upper() + text[1:] if text else text


def lc_first(text):
    """Lower-case the first letter unless it starts an acronym («API», «PDF»)."""
    if not text:
        return text
    if len(text) > 1 and text[1].isupper():
        return text
    return text[:1].lower() + text[1:]


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def count_words(n, one, few, many):
    return "%d %s" % (n, plural(n, one, few, many))


def agent_name(agent):
    if not agent:
        return "агент"
    return AGENT_NAMES.get(agent, agent)


def quoted(text):
    return "«%s»" % text


def join_and(items):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " и " + items[-1]
