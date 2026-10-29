"""
Оповіщення про повітряну тривогу: UkraineAlarm API -> Telegram-канал офісу.
Район моніторингу: м. Могилів-Подільський (лише він, без області/району вище).
Реагує тільки на тип AIR (повітряна тривога).

Змінні оточення:
  UKRAINEALARM_API_KEY  - ключ API
  TG_BOT_TOKEN          - токен бота (від @BotFather); бот має бути адміном каналу
  TG_CHAT_ID            - @username каналу або числовий id (-100...)
  REGION_IDS            - (рекомендовано після --find) ID через кому; якщо задано, пошук за назвою не робиться
  REGION_NAME           - назва для пошуку, за замовчуванням "Могилів-Подільськ"
  POLL_SECONDS          - інтервал опитування, за замовчуванням 30

Запуск:
  python alert_bot.py --find   # показати знайдені регіони та ID, нічого не слати
  python alert_bot.py --test   # надіслати тестове повідомлення в Telegram
  python alert_bot.py          # основний цикл
"""
import os
import sys
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")


def fmt_duration(delta):
    """Формат тривалості: '1 год 12 хв' або '23 хв'."""
    minutes = max(1, round(delta.total_seconds() / 60))
    h, m = divmod(minutes, 60)
    return f"{h} год {m} хв" if h else f"{m} хв"

API = "https://api.ukrainealarm.com/api/v3"
def _env(name):
    """Читає змінну, прибирає пробіли, переноси рядків і випадкові лапки."""
    raw = os.environ.get(name, "").strip().strip('"').strip("'")
    # Ключ UkraineAlarm має вигляд "номер:код". При копіюванні з листа між частинами
    # часто з'являється пробіл/перенос рядка - видаляємо будь-які пробільні символи всередині.
    return "".join(raw.split())


KEY = _env("UKRAINEALARM_API_KEY")
TG_TOKEN = _env("TG_BOT_TOKEN")
TG_CHAT = _env("TG_CHAT_ID")
NAME = os.environ.get("REGION_NAME", "Могилів-Подільськ")
POLL = int(os.environ.get("POLL_SECONDS", "30"))
ALERT_TYPE = "AIR"

HEADERS = {"Authorization": KEY}


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


def walk(node, path, found):
    """Обходить дерево регіонів; збирає шляхи (область > район > громада) до вузлів, назва яких містить NAME."""
    if isinstance(node, list):
        for n in node:
            walk(n, path, found)
        return
    if not isinstance(node, dict):
        return
    here = path + [node] if "regionId" in node else path
    if "regionName" in node and NAME.lower() in node["regionName"].lower():
        found.append(here)
    for key in ("states", "regionChildIds", "children"):
        child = node.get(key)
        if isinstance(child, list):
            walk(child, here, found)


def resolve_region_ids():
    """Повертає ID лише самого знайденого вузла (громада/місто), без області та району вище."""
    # За замовчуванням: 33 = Могилів-Подільський район, 172 = м. Могилів-Подільський (громада)
    env_ids = os.environ.get("REGION_IDS", "33,172").strip()
    if env_ids:
        return {i.strip() for i in env_ids.split(",") if i.strip()}, f"REGION_IDS={env_ids}"
    tree = api_get("/regions")
    found = []
    walk(tree, [], found)
    if not found:
        sys.exit(f"Регіон за назвою '{NAME}' не знайдено. Подивіться /regions у swagger "
                 f"і задайте REGION_IDS вручну.")
    ids, desc = set(), []
    for chain in found:
        ids.add(str(chain[-1]["regionId"]))  # тільки сам вузол
        desc.append(" > ".join(f'{n["regionName"]} [{n["regionId"]}]' for n in chain)
                    + f"   <- моніторимо ID {chain[-1]['regionId']}")
    return ids, "\n".join(desc)


def tg_send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text},
        timeout=15,
    )
    r.raise_for_status()


def air_alert_active(ids):
    """True, якщо в одному з наших регіонів є активна тривога типу AIR."""
    data = api_get("/alerts")
    regions = data if isinstance(data, list) else [data]
    for reg in regions:
        for a in reg.get("activeAlerts", []):
            rid = str(a.get("regionId", reg.get("regionId")))
            if rid in ids and a.get("type") == ALERT_TYPE:
                return True
    return False


def main():
    if not KEY:
        sys.exit("Не задано UKRAINEALARM_API_KEY")
    ids, desc = resolve_region_ids()
    print("Моніторимо:\n" + desc, flush=True)

    if "--find" in sys.argv:
        return
    if not (TG_TOKEN and TG_CHAT):
        sys.exit("Не задано TG_BOT_TOKEN / TG_CHAT_ID")
    if "--test" in sys.argv:
        tg_send("✅ Тест: бот оповіщення про тривогу підключено до каналу.")
        print("Тестове повідомлення надіслано.")
        return

    was_active = None  # None = перший цикл
    started_at = None  # момент початку тривоги (час, коли бот її помітив)
    while True:
        try:
            now_active = air_alert_active(ids)
            if now_active and was_active is not True:
                started_at = datetime.now(TZ)
                tg_send(f"🚨 Повітряна тривога в м. Могилів-Подільський ({started_at:%H:%M}). "
                        "Дійте згідно з інструкцією.")
            elif was_active is True and not now_active:
                ended_at = datetime.now(TZ)
                if started_at:
                    tg_send(f"✅ Відбій повітряної тривоги в м. Могилів-Подільський "
                            f"({ended_at:%H:%M}).\n"
                            f"Тривала {fmt_duration(ended_at - started_at)} "
                            f"(з {started_at:%H:%M}).")
                else:
                    tg_send(f"✅ Відбій повітряної тривоги в м. Могилів-Подільський "
                            f"({ended_at:%H:%M}).")
                started_at = None
            was_active = now_active  # оновлюємо лише після успішної відправки
        except requests.RequestException as e:
            print("Помилка запиту:", e, file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
