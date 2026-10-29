"""
Оповіщення про повітряну тривогу: UkraineAlarm API -> Telegram-канал.
Моніторить кілька міст (див. PLACES), реагує тільки на тип AIR (повітряна тривога).
Показує рівень тривоги, час і тривалість на відбої, раз на добу надсилає підсумок.

Змінні оточення:
  UKRAINEALARM_API_KEY  - ключ API
  TG_BOT_TOKEN          - токен бота (від @BotFather); бот має бути адміном каналу
  TG_CHAT_ID            - @username каналу або числовий id (-100...)
  POLL_SECONDS          - інтервал опитування, за замовчуванням 30
  STATE_FILE            - файл журналу тривог (на Railway - шлях на Volume, напр. /data/alerts_state.json)

Запуск:
  python alert_bot.py --find      # показати, які регіони моніторяться
  python alert_bot.py --test      # надіслати тестове повідомлення в Telegram
  python alert_bot.py --summary   # надіслати проміжний підсумок за сьогодні
  python alert_bot.py             # основний цикл
"""
import json
import os
import sys
import time
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Kyiv")

# Місто -> ID регіонів у UkraineAlarm (район + громада). ID взято з /api/v3/regions.
# Область не моніторимо: тривоги по всій області не повинні спрацьовувати.
PLACES = {
    "м. Могилів-Подільський": {"33", "172"},     # Могилів-Подільський район, місто/громада
    "м. Сторожинець": {"137", "1536"},            # Чернівецький район, Сторожинецька громада
    "м. Герца": {"137", "1518"},                  # Чернівецький район, Герцаївська громада
    "м. Кельменці": {"139", "1546"},              # Дністровський район, Кельменецька громада
    "м. Сокиряни": {"139", "1553"},               # Дністровський район, Сокирянська громада
    "м. Новодністровськ": {"139", "1551"},        # Дністровський район, Новодністровська громада
}

API = "https://api.ukrainealarm.com/api/v3"
ALERT_TYPE = "AIR"
POLL = int(os.environ.get("POLL_SECONDS", "30"))
STATE_FILE = os.environ.get("STATE_FILE", "alerts_state.json")


def _env(name):
    """Читає змінну, прибирає пробіли, переноси рядків і випадкові лапки."""
    raw = os.environ.get(name, "").strip().strip('"').strip("'")
    # Ключ UkraineAlarm має вигляд "номер:код"; при копіюванні з листа всередині
    # часто з'являється пробіл/перенос рядка - видаляємо будь-які пробільні символи.
    return "".join(raw.split())


KEY = _env("UKRAINEALARM_API_KEY")
TG_TOKEN = _env("TG_BOT_TOKEN")
TG_CHAT = _env("TG_CHAT_ID")
HEADERS = {"Authorization": KEY}

LEVEL_PRIORITY = ["Red", "Orange", "Yellow"]  # від вищого до нижчого; невідомі - в кінці
LEVEL_LABELS = {
    "Red": "🔴 червоний рівень",
    "Orange": "🟠 помаранчевий рівень",
    "Yellow": "🟡 жовтий рівень",
}


def fmt_duration(delta):
    """Формат тривалості: '1 год 12 хв' або '23 хв'."""
    minutes = max(1, round(delta.total_seconds() / 60))
    h, m = divmod(minutes, 60)
    return f"{h} год {m} хв" if h else f"{m} хв"


def level_text(level):
    if not level:
        return ""
    return LEVEL_LABELS.get(level, f"рівень: {level}")


def api_get(path):
    r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code in (401, 403):
        # Не падаємо в нескінченний рестарт: пояснюємо і чекаємо.
        print(f"API відхилило ключ (HTTP {r.status_code}) на {path}. Довжина ключа: {len(KEY)}. "
              f"Перевірте UKRAINEALARM_API_KEY у Variables (без лапок/пробілів) "
              f"і що ключ активований на api.ukrainealarm.com.", file=sys.stderr, flush=True)
        time.sleep(300)
        sys.exit(1)
    r.raise_for_status()
    return r.json()


def tg_send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text},
        timeout=15,
    )
    r.raise_for_status()


def air_alert_states():
    """Один запит до API -> {місто: None (тривоги немає) | "" (є, рівень не вказано) | "Red"/"Yellow"/...}."""
    data = api_get("/alerts")
    regions = data if isinstance(data, list) else [data]
    found = []  # (regionId, set(levels)) для активних AIR-тривог
    for reg in regions:
        for a in reg.get("activeAlerts", []):
            if a.get("type") != ALERT_TYPE:
                continue
            rid = str(a.get("regionId", reg.get("regionId")))
            levels = {lv["alertLevel"] for lv in (a.get("activeAlertLevels") or [])
                      if lv.get("alertLevel")}
            found.append((rid, levels))

    result = {}
    for name, ids in PLACES.items():
        active, levels = False, set()
        for rid, lv in found:
            if rid in ids:
                active = True
                levels |= lv
        if not active:
            result[name] = None
        elif not levels:
            result[name] = ""
        else:
            result[name] = min(levels, key=lambda x: LEVEL_PRIORITY.index(x)
                               if x in LEVEL_PRIORITY else 99)
    return result


# ---------- журнал тривог ----------

def load_state():
    """{"places": {місто: {"open": {...}|None, "events": [{"start","end"}]}}, "summary_day": "YYYY-MM-DD"}"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("places", {})
    st.setdefault("summary_day", None)
    # міграція зі старого формату (одне місто)
    if "events" in st and "м. Могилів-Подільський" not in st["places"]:
        st["places"]["м. Могилів-Подільський"] = {"open": st.pop("open", None),
                                                  "events": st.pop("events")}
    for name in PLACES:
        pd = st["places"].setdefault(name, {})
        pd.setdefault("open", None)
        pd.setdefault("events", [])
    return st


def save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print("Не вдалося зберегти журнал:", e, file=sys.stderr, flush=True)


def place_day_body(pd, day):
    """Текст підсумку одного міста за добу `day`. Тривалості обрізаються межами доби."""
    start = datetime.combine(day, dtime.min, TZ)
    end = start + timedelta(days=1)
    items = [(datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
             for e in pd["events"]]
    if pd.get("open"):
        items.append((datetime.fromisoformat(pd["open"]["start"]), datetime.now(TZ)))
    spans = []
    for s, e in items:
        s2, e2 = max(s, start), min(e, end)
        if e2 > s2:
            spans.append((s2, e2))
    spans.sort()
    if not spans:
        return "Повітряних тривог не було."

    total = sum((e - s for s, e in spans), timedelta())
    pct = 100 * total.total_seconds() / (end - start).total_seconds()
    longest = max(spans, key=lambda x: x[1] - x[0])
    lines = [
        f"Тривог: {len(spans)}",
        f"Загальна тривалість: {fmt_duration(total)} "
        f"({'<1' if pct < 1 else round(pct)}% доби)",
        f"Найдовша: {fmt_duration(longest[1] - longest[0])} "
        f"({longest[0]:%H:%M}–{longest[1]:%H:%M})",
    ]
    for i, (s, e) in enumerate(spans[:10], 1):
        lines.append(f"{i}) {s:%H:%M}–{e:%H:%M} ({fmt_duration(e - s)})")
    if len(spans) > 10:
        lines.append(f"… і ще {len(spans) - 10}")
    return "\n".join(lines)


def day_summary_messages(st, day):
    """Підсумок за добу. Міста з однаковим результатом (спільний район) об'єднуються в один блок."""
    head = f"📊 Підсумок за {day:%d.%m.%Y}"
    groups = {}
    for name in PLACES:
        groups.setdefault(place_day_body(st["places"][name], day), []).append(name)
    parts = [f"📍 {', '.join(names)}\n{body}" for body, names in groups.items()]

    messages, cur = [], head
    for p in parts:
        if len(cur) + len(p) + 2 > 3500:  # ліміт Telegram 4096 символів
            messages.append(cur)
            cur = head + " (продовження)"
        cur += "\n\n" + p
    messages.append(cur)
    return messages


# ---------- основний цикл ----------

def group_by(items, key):
    groups = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    return groups.values()


def main():
    if not KEY:
        sys.exit("Не задано UKRAINEALARM_API_KEY")
    print("Моніторимо:\n" + "\n".join(f"  {n}: ID {', '.join(sorted(ids))}"
                                      for n, ids in PLACES.items()), flush=True)
    if "--find" in sys.argv:
        return
    if not (TG_TOKEN and TG_CHAT):
        sys.exit("Не задано TG_BOT_TOKEN / TG_CHAT_ID")
    if "--test" in sys.argv:
        tg_send("✅ Тест: бот оповіщення про тривогу підключено до каналу.")
        print("Тестове повідомлення надіслано.")
        return

    st = load_state()
    if "--summary" in sys.argv:
        for m in day_summary_messages(st, datetime.now(TZ).date()):
            tg_send(m + "\n\n(проміжний підсумок за сьогодні)")
        print("Підсумок надіслано.")
        return

    # Відновлюємо стан після рестарту: якщо тривога вже йшла - не надсилаємо "початок" вдруге.
    rt = {}
    for name in PLACES:
        op = st["places"][name]["open"]
        rt[name] = ({"active": True, "level": op.get("level"),
                     "start": datetime.fromisoformat(op["start"])} if op
                    else {"active": False, "level": None, "start": None})
    if not st["summary_day"]:
        st["summary_day"] = datetime.now(TZ).date().isoformat()
        save_state(st)

    while True:
        try:
            states = air_alert_states()
            now = datetime.now(TZ)
            starts, changes, ends = [], [], []
            for name in PLACES:
                s, r = states[name], rt[name]
                if s is not None and not r["active"]:
                    starts.append((name, s))
                elif s is not None and r["active"] and s != r["level"]:
                    changes.append((name, s))
                elif s is None and r["active"]:
                    ends.append((name, r["start"]))

            # Початок: міста з однаковим рівнем - одним повідомленням.
            for grp in group_by(starts, key=lambda x: x[1]):
                level = grp[0][1]
                names = ", ".join(n for n, _ in grp)
                lt = level_text(level)
                tg_send(f"🚨 Повітряна тривога: {names} ({now:%H:%M})"
                        + (f", {lt}" if lt else "") + ". Дійте згідно з інструкцією.")
                for n, _ in grp:  # стан оновлюємо лише після успішної відправки
                    rt[n] = {"active": True, "level": level, "start": now}
                    st["places"][n]["open"] = {"start": now.isoformat(), "level": level}
                save_state(st)

            # Зміна рівня під час тривоги (наприклад, жовтий -> червоний).
            for grp in group_by(changes, key=lambda x: x[1]):
                level = grp[0][1]
                names = ", ".join(n for n, _ in grp)
                tg_send(f"⚠️ Змінився рівень тривоги: {names} ({now:%H:%M}): "
                        f"{level_text(level) or 'рівень не вказано'}.")
                for n, _ in grp:
                    rt[n]["level"] = level
                    if st["places"][n]["open"]:
                        st["places"][n]["open"]["level"] = level
                save_state(st)

            # Відбій: групуємо міста з однаковим початком і тривалістю.
            for grp in group_by(ends, key=lambda x: (f"{x[1]:%H:%M}", fmt_duration(now - x[1]))):
                started = grp[0][1]
                names = ", ".join(n for n, _ in grp)
                tg_send(f"✅ Відбій повітряної тривоги: {names} ({now:%H:%M}).\n"
                        f"Тривала {fmt_duration(now - started)} (з {started:%H:%M}).")
                for n, s0 in grp:
                    st["places"][n]["events"].append({"start": s0.isoformat(),
                                                      "end": now.isoformat()})
                    st["places"][n]["open"] = None
                    rt[n] = {"active": False, "level": None, "start": None}
                save_state(st)

            # Підсумок за добу: надсилається одразу після опівночі за попередній день.
            today = now.date()
            if st["summary_day"] != today.isoformat():
                prev = date.fromisoformat(st["summary_day"])
                for m in day_summary_messages(st, prev):
                    tg_send(m)
                st["summary_day"] = today.isoformat()
                cutoff = (now - timedelta(days=40)).isoformat()
                for pd in st["places"].values():
                    pd["events"] = [e for e in pd["events"] if e["end"] >= cutoff]
                save_state(st)
        except requests.RequestException as e:
            print("Помилка запиту:", e, file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
