import csv
import datetime as dt
import json
import sys
from pathlib import Path

import jsonschema
import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import export  # noqa: E402

SCHEMA_DIR = ROOT / "data" / "v1" / "schema"
NOW = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(days=1)
NAMES = {"USD": (840, "Долар США", "US Dollar"), "EUR": (978, "Євро", "Euro"), "PLN": (985, "Злотий", "Zloty")}


def entry(code, date, rate, units=1, rpu=True):
    r030, txt, enname = NAMES[code]
    e = {"exchangedate": date, "cc": code, "r030": r030, "txt": txt, "enname": enname, "rate": rate, "units": units}
    if rpu:
        e["rate_per_unit"] = round(rate / units, 8)
    return e


def recent_days(code, base, days=("20.10.2026", "21.10.2026", "22.10.2026", "26.10.2026")):
    return [entry(code, d, round(base + i * 0.1, 4)) for i, d in enumerate(days)]


def parse(date):
    return dt.datetime.strptime(date, "%d.%m.%Y").date()


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeNbu:
    """exchange_site: з valcode — одна валюта, без нього — усі; завжди в межах start…end."""

    def __init__(self):
        self.data = {
            "USD": [
                entry("USD", "01.09.1996", 179900),        # карбованці — мають відкинутись
                entry("USD", "02.09.1996", 176.1, units=100),
                entry("USD", "28.12.2019", 23.6862),
                *recent_days("USD", 41.0),
            ],
            "EUR": [entry("EUR", "28.12.2019", 26.422), *recent_days("EUR", 48.0)],
        }
        self.calls = []

    def get(self, url, params=None, timeout=None):
        assert url == export.NBU_RATES_URL
        assert timeout, "кожен запит до НБУ має мати таймаут"
        self.calls.append(params)
        start = dt.datetime.strptime(params["start"], "%Y%m%d").date()
        end = dt.datetime.strptime(params["end"], "%Y%m%d").date()
        codes = [params["valcode"].upper()] if "valcode" in params else sorted(self.data)
        rows = [e for c in codes for e in self.data.get(c, []) if start <= parse(e["exchangedate"]) <= end]
        return FakeResponse(sorted(rows, key=lambda e: parse(e["exchangedate"])))

    def set_rate(self, code, date, rate):
        for e in self.data[code]:
            if e["exchangedate"] == date:
                e["rate"] = e["rate_per_unit"] = rate


@pytest.fixture
def nbu(monkeypatch):
    fake = FakeNbu()
    monkeypatch.setattr(export.requests, "get", fake.get)
    monkeypatch.setattr(export, "sleep", lambda s: None)
    return fake


def run(tmp_path, now=NOW, full=False):
    logs = []
    changed = export.export(tmp_path, now=now, full=full, log=logs.append)
    return changed, logs


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rates(tmp_path, code):
    return {r["date"]: r["rate"] for r in read_json(tmp_path / "v1" / f"{code}.json")["rates"]}


def snapshot(tmp_path):
    return {p.name: p.read_bytes() for p in (tmp_path / "v1").iterdir()}


def validate(path_or_doc, schema_name):
    doc = read_json(path_or_doc) if isinstance(path_or_doc, Path) else path_or_doc
    schema = read_json(SCHEMA_DIR / f"{schema_name}.schema.json")
    jsonschema.validate(doc, schema, format_checker=jsonschema.FormatChecker())


# ---------- НБУ ----------

def test_nbu_get_retries_then_succeeds(monkeypatch):
    responses = [requests.ConnectionError("down"), FakeResponse([], status=500), FakeResponse(["ok"])]
    pauses = []

    def fake_get(url, params=None, timeout=None):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(export.requests, "get", fake_get)
    monkeypatch.setattr(export, "sleep", pauses.append)
    assert export.nbu_get("https://nbu") == ["ok"]
    assert pauses == [1, 2]


def test_nbu_get_gives_up(monkeypatch):
    monkeypatch.setattr(export.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("slow")))
    monkeypatch.setattr(export, "sleep", lambda s: None)
    with pytest.raises(export.ExportError, match="НБУ не відповідає"):
        export.nbu_get("https://nbu")


def test_fetch_rates_params(nbu):
    export.fetch_rates(dt.date(2026, 10, 9), dt.date(2026, 11, 2))
    export.fetch_rates(dt.date(1996, 9, 2), dt.date(2026, 11, 2), "USD")
    common = {"sort": "exchangedate", "order": "asc", "json": ""}
    assert nbu.calls == [
        {"start": "20261009", "end": "20261102", **common},
        {"start": "19960902", "end": "20261102", **common, "valcode": "usd"},
    ]


def test_to_row_computes_rate_per_unit_when_missing():
    row = export.to_row(entry("USD", "28.12.2019", 2368.62, units=100, rpu=False))
    assert row == {"date": "2019-12-28", "rate": 2368.62, "units": 100, "rate_per_unit": 23.6862}
    assert export.to_row({"exchangedate": "01.01.2020", "rate": 5})["units"] == 1


def test_group_by_currency_cleans_input():
    grouped = export.group_by_currency([
        entry("USD", "01.09.1996", 179900),
        entry("USD", "02.09.1996", 176.1, units=100),
        entry("USD", "02.09.1996", 176.2, units=100),  # дубль дати — береться останній
        {"cc": "USD", "exchangedate": "03.09.1996", "rate": None},
        {"exchangedate": "03.09.1996", "rate": 1.0},
        entry("EUR", "28.12.2019", 26.422),
    ])
    assert sorted(grouped) == ["EUR", "USD"]
    assert grouped["USD"]["rows"] == {"1996-09-02": {"date": "1996-09-02", "rate": 176.2, "units": 100,
                                                     "rate_per_unit": 1.762}}
    assert grouped["USD"]["meta"] == {"r030": 840, "name": "Долар США", "name_en": "US Dollar"}


# ---------- Перший запуск ----------

def test_first_run_backfills_every_currency(nbu, tmp_path):
    changed, logs = run(tmp_path)
    assert changed == ["EUR", "USD"]
    assert logs == ["Запитів до НБУ: 3, валют: 2, змінено: EUR, USD"]
    assert [c.get("valcode") for c in nbu.calls] == [None, "eur", "usd"]
    assert nbu.calls[0]["start"] == "20261009"  # сьогодні мінус RECHECK_DAYS

    root = tmp_path / "v1"
    usd = read_json(root / "USD.json")
    assert usd["updated_at"] == "2026-10-23T14:00:00Z"
    assert [r["date"] for r in usd["rates"]] == ["1996-09-02", "2019-12-28", "2026-10-20", "2026-10-21",
                                                 "2026-10-22", "2026-10-26"]
    # один запис на рядок — git-диф показує лише нові дні
    assert '\n    {"date": "2019-12-28", "rate": 23.6862, "units": 1, "rate_per_unit": 23.6862},\n' in \
        (root / "USD.json").read_text(encoding="utf-8")

    with open(root / "USD.csv", encoding="utf-8") as f:
        assert list(csv.reader(f))[:2] == [["date", "rate", "units", "rate_per_unit"],
                                           ["1996-09-02", "176.1", "100", "1.761"]]

    index = read_json(root / "index.json")
    assert index["last_date"] == "2026-10-26"
    assert index["currencies"][1] == {
        "code": "USD", "r030": 840, "name": "Долар США", "name_en": "US Dollar", "units": 1,
        "first_date": "1996-09-02", "last_date": "2026-10-26", "quoted": True,
        "updated_at": "2026-10-23T14:00:00Z",
    }
    latest = read_json(root / "latest.json")
    assert [(r["code"], r["date"]) for r in latest["rates"]] == [("EUR", "2026-10-26"), ("USD", "2026-10-26")]

    for name, schema in (("USD.json", "currency"), ("index.json", "index"), ("latest.json", "latest")):
        validate(root / name, schema)


# ---------- Щоденне оновлення ----------

def test_daily_run_is_one_request_and_changes_nothing(nbu, tmp_path):
    run(tmp_path)
    before = snapshot(tmp_path)
    nbu.calls.clear()
    changed, logs = run(tmp_path, now=LATER)
    assert len(nbu.calls) == 1 and "valcode" not in nbu.calls[0]
    assert nbu.calls[0]["start"] == "20261010"  # сьогодні (24.10) мінус RECHECK_DAYS: курс на 26.10 встановлено наперед
    assert changed == []
    assert logs == ["Запитів до НБУ: 1, валют: 2, змінено: нічого"]
    assert snapshot(tmp_path) == before


def test_new_day_is_appended(nbu, tmp_path):
    run(tmp_path)
    nbu.data["USD"].append(entry("USD", "27.10.2026", 41.9))
    changed, _ = run(tmp_path, now=LATER)
    assert changed == ["USD"]
    assert rates(tmp_path, "USD")["2026-10-27"] == 41.9
    assert read_json(tmp_path / "v1" / "USD.json")["updated_at"] == "2026-10-24T14:00:00Z"
    assert read_json(tmp_path / "v1" / "EUR.json")["updated_at"] == "2026-10-23T14:00:00Z"
    assert read_json(tmp_path / "v1" / "index.json")["last_date"] == "2026-10-27"


def test_recent_correction_is_picked_up(nbu, tmp_path):
    run(tmp_path)
    nbu.set_rate("EUR", "21.10.2026", 48.55)
    assert run(tmp_path, now=LATER)[0] == ["EUR"]
    assert rates(tmp_path, "EUR")["2026-10-21"] == 48.55


def test_old_correction_needs_full_run(nbu, tmp_path):
    run(tmp_path)
    nbu.set_rate("USD", "28.12.2019", 23.7)
    assert run(tmp_path, now=LATER)[0] == []  # поза вікном перевірки
    assert rates(tmp_path, "USD")["2019-12-28"] == 23.6862
    changed, logs = run(tmp_path, now=LATER, full=True)
    assert changed == ["USD"]
    assert logs[0].startswith("Запитів до НБУ: 3,")
    assert rates(tmp_path, "USD")["2019-12-28"] == 23.7


def test_new_currency_is_backfilled(nbu, tmp_path):
    run(tmp_path)
    nbu.data["PLN"] = [entry("PLN", "02.09.1996", 30.0, units=100), *recent_days("PLN", 11.0)]
    nbu.calls.clear()
    changed, _ = run(tmp_path, now=LATER)
    assert changed == ["PLN"]
    assert [c.get("valcode") for c in nbu.calls] == [None, "pln"]
    assert rates(tmp_path, "PLN")["1996-09-02"] == 30.0


def test_currency_no_longer_quoted_is_kept(nbu, tmp_path):
    run(tmp_path)
    nbu.data["EUR"] = [e for e in nbu.data["EUR"] if e["exchangedate"] != "26.10.2026"]
    run(tmp_path, now=LATER)
    index = read_json(tmp_path / "v1" / "index.json")
    assert [(c["code"], c["quoted"]) for c in index["currencies"]] == [("EUR", False), ("USD", True)]
    assert [r["code"] for r in read_json(tmp_path / "v1" / "latest.json")["rates"]] == ["USD"]
    assert "2026-10-26" in rates(tmp_path, "EUR")  # вже записане не видаляємо


# ---------- Збої НБУ ----------

def test_empty_window_fails(nbu, tmp_path):
    nbu.data = {}
    with pytest.raises(export.ExportError, match="жодного курсу"):
        run(tmp_path)


def test_missing_history_for_new_currency_fails(nbu, tmp_path, monkeypatch):
    original = nbu.get
    monkeypatch.setattr(export.requests, "get",
                        lambda url, params=None, timeout=None:
                        FakeResponse([]) if "valcode" in params else original(url, params, timeout))
    with pytest.raises(export.ExportError, match="EUR: НБУ не повернув історію"):
        run(tmp_path)


def test_shrunk_full_history_fails_and_keeps_data(nbu, tmp_path):
    run(tmp_path)
    before = snapshot(tmp_path)
    nbu.data["USD"] = nbu.data["USD"][3:]
    with pytest.raises(export.ExportError, match="збій НБУ"):
        run(tmp_path, now=LATER, full=True)
    assert snapshot(tmp_path) == before


def test_main(nbu, tmp_path, capsys, monkeypatch):
    seen = []
    real_export = export.export
    monkeypatch.setattr(export, "export", lambda out, full: seen.append(full) or real_export(out, full=full))
    assert export.main(["--out", str(tmp_path)]) == 0
    assert export.main(["--out", str(tmp_path), "--full"]) == 0
    assert seen == [False, True]
    nbu.data = {}
    assert export.main(["--out", str(tmp_path / "empty")]) == 1
    assert "Помилка: НБУ не повернув жодного курсу" in capsys.readouterr().err


# ---------- Опубліковані дані ----------

PUBLISHED = ROOT / "data" / "v1"


@pytest.mark.skipif(not (PUBLISHED / "index.json").exists(), reason="дані ще не згенеровано")
def test_published_data_matches_schemas():
    validate(PUBLISHED / "index.json", "index")
    validate(PUBLISHED / "latest.json", "latest")
    index = read_json(PUBLISHED / "index.json")
    for currency in index["currencies"]:
        path = PUBLISHED / f"{currency['code']}.json"
        doc = read_json(path)
        rows = doc["rates"]
        # jsonschema повільний на сотнях тисяч рядків: схемою перевіряємо вибірку, решту — напряму
        validate({**doc, "rates": rows[:50] + rows[50:-50:97] + rows[-50:]}, "currency")
        assert all(r.keys() == {"date", "rate", "units", "rate_per_unit"} for r in rows), path.name
        assert all(r["rate"] > 0 and r["rate_per_unit"] > 0 and type(r["units"]) is int and r["units"] >= 1
                   for r in rows), path.name
        dates = [r["date"] for r in rows]
        assert dates == sorted(set(dates)), f"{path.name}: дати не впорядковані або дублюються"
        assert dates[0] >= export.HRYVNIA_DAY.isoformat()
        assert (rows[0]["date"], rows[-1]["date"]) == (currency["first_date"], currency["last_date"])
