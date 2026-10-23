"""Експорт офіційних курсів гривні від НБУ у статичні файли формату v1.

Щоденне оновлення — один запит до НБУ: усі валюти за останні RECHECK_DAYS днів плюс
курси, встановлені наперед. Нові дні дописуються, недавні перевіряються на виправлення.
Повна історія завантажується лише для валют, яких ще немає у файлах, або з прапорцем --full.
"""

import argparse
import csv
import datetime as dt
import io
import json
import sys
import time
from pathlib import Path

import requests

NBU_RATES_URL = "https://bank.gov.ua/NBU_Exchange/exchange_site"
SCHEMA = 1
HRYVNIA_DAY = dt.date(1996, 9, 2)  # грошова реформа: раніше курси встановлювались у карбованцях
RECHECK_DAYS = 14  # НБУ зрідка виправляє недавні курси — їх перечитуємо щоразу
LOOKAHEAD_DAYS = 10  # НБУ встановлює курс наперед — на наступний робочий день
HTTP_TIMEOUT = 60
RETRIES = 3
REQUEST_PAUSE = 0.3
CSV_FIELDS = ["date", "rate", "units", "rate_per_unit"]
INDEX_FILES = {"index.json", "latest.json"}

sleep = time.sleep  # підміняється в тестах


class ExportError(RuntimeError):
    pass


# ---------- НБУ ----------

def nbu_get(url, params=None):
    error = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            error = e
            sleep(2 ** attempt)
    raise ExportError(f"НБУ не відповідає ({url}): {error}")


def fetch_rates(start, end, code=None):
    """Курси за проміжок: однієї валюти або, без `code`, усіх одразу."""
    params = {
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "sort": "exchangedate",
        "order": "asc",
        "json": "",
    }
    if code:
        params["valcode"] = code.lower()
    return nbu_get(NBU_RATES_URL, params)


def to_row(entry):
    day, month, year = entry["exchangedate"].split(".")
    units = entry.get("units") or 1
    rate_per_unit = entry.get("rate_per_unit")
    if rate_per_unit is None:
        rate_per_unit = round(entry["rate"] / units, 8)
    return {"date": f"{year}-{month}-{day}", "rate": entry["rate"], "units": units, "rate_per_unit": rate_per_unit}


def group_by_currency(entries):
    """{code: {"meta": {...}, "rows": {date: row}}} — без карбованців, неповних записів і дублів дат."""
    grouped = {}
    for entry in entries:
        if not entry.get("cc") or not entry.get("exchangedate") or entry.get("rate") is None:
            continue
        row = to_row(entry)
        if row["date"] < HRYVNIA_DAY.isoformat():
            continue
        item = grouped.setdefault(entry["cc"], {"meta": None, "rows": {}})
        item["rows"][row["date"]] = row
        item["meta"] = {"r030": entry.get("r030"), "name": entry.get("txt"), "name_en": entry.get("enname")}
    return grouped


# ---------- Файли ----------

def currency_json(code, meta, rows, updated_at):
    """JSON з одним записом на рядок — щоб диф у git показував лише нові дні."""
    head = {"schema": SCHEMA, "code": code, **meta, "updated_at": updated_at}
    head_text = json.dumps(head, ensure_ascii=False, indent=2)[:-2]
    lines = ",\n    ".join(json.dumps(r, ensure_ascii=False, separators=(", ", ": ")) for r in rows)
    return f'{head_text},\n  "rates": [\n    {lines}\n  ]\n}}\n'


def currency_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_if_changed(path, text):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def load_existing(root):
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.glob("*.json"))
        if p.name not in INDEX_FILES
    }


# ---------- Експорт ----------

def export(out_dir, now=None, full=False, log=print):
    now = now or dt.datetime.now(dt.timezone.utc)
    updated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.date() + dt.timedelta(days=LOOKAHEAD_DAYS)
    root = Path(out_dir) / f"v{SCHEMA}"
    root.mkdir(parents=True, exist_ok=True)
    existing = load_existing(root)

    # 1. Один запит: усі валюти за останні дні — і нові курси, і перелік валют, які НБУ котирує
    last_known = max((dt.date.fromisoformat(d["rates"][-1]["date"]) for d in existing.values()), default=now.date())
    since = min(last_known, now.date()) - dt.timedelta(days=RECHECK_DAYS)
    recent = group_by_currency(fetch_rates(since, until))
    if not recent:
        raise ExportError(f"НБУ не повернув жодного курсу за {since}…{until}")
    newest_date = max(date for item in recent.values() for date in item["rows"])

    # 2. Повна історія — лише для нових валют (або всіх, якщо --full)
    backfill = sorted(set(recent) | set(existing)) if full else sorted(set(recent) - set(existing))
    history = {}
    for i, code in enumerate(backfill):
        if i:
            sleep(REQUEST_PAUSE)
        history[code] = group_by_currency(fetch_rates(HRYVNIA_DAY, until, code)).get(code)
        if history[code] is None:
            raise ExportError(f"{code}: НБУ не повернув історію валюти")

    currencies, latest, changed = [], [], []
    for code in sorted(set(recent) | set(existing)):
        doc = existing.get(code)
        rows = {} if code in history or doc is None else {r["date"]: r for r in doc["rates"]}
        meta = {k: doc[k] for k in ("r030", "name", "name_en")} if doc else None
        for source in (history.get(code), recent.get(code)):
            if source:  # свіжі дані перекривають старі
                rows.update(source["rows"])
                meta = source["meta"]
        rows = [rows[d] for d in sorted(rows)]
        if doc and len(rows) < len(doc["rates"]):
            raise ExportError(f"{code}: НБУ повернув {len(rows)} рядків, а у файлі {len(doc['rates'])} — "
                              "схоже на збій НБУ, дані не перезаписую")

        if doc and doc["rates"] == rows and all(doc.get(k) == v for k, v in meta.items()):
            stamp = doc["updated_at"]
        else:
            stamp = updated_at
            changed.append(code)
        write_if_changed(root / f"{code}.json", currency_json(code, meta, rows, stamp))
        write_if_changed(root / f"{code}.csv", currency_csv(rows))

        last = rows[-1]
        quoted = newest_date in recent.get(code, {"rows": {}})["rows"]
        currencies.append({
            "code": code, **meta, "units": last["units"],
            "first_date": rows[0]["date"], "last_date": last["date"],
            "quoted": quoted, "updated_at": stamp,
        })
        if quoted:
            latest.append({"code": code, **last})

    data_updated = max(c["updated_at"] for c in currencies)
    write_if_changed(root / "index.json", dump({
        "schema": SCHEMA,
        "source": "https://bank.gov.ua",
        "updated_at": data_updated,
        "last_date": max(c["last_date"] for c in currencies),
        "currencies": currencies,
    }))
    write_if_changed(root / "latest.json", dump({
        "schema": SCHEMA,
        "updated_at": data_updated,
        "rates": latest,
    }))
    log(f"Запитів до НБУ: {1 + len(backfill)}, валют: {len(currencies)}, змінено: {', '.join(changed) or 'нічого'}")
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data", help="тека для файлів (за замовчуванням data)")
    parser.add_argument("--full", action="store_true", help="перезавантажити повну історію всіх валют")
    args = parser.parse_args(argv)
    try:
        export(args.out, full=args.full)
    except ExportError as e:
        print(f"Помилка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
