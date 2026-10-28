# Офіційні курси гривні НБУ — відкриті дані

[![tests](https://github.com/EnjoyFX/uarates_data/actions/workflows/tests.yml/badge.svg)](https://github.com/EnjoyFX/uarates_data/actions/workflows/tests.yml)
[![update](https://github.com/EnjoyFX/uarates_data/actions/workflows/update.yml/badge.svg)](https://github.com/EnjoyFX/uarates_data/actions/workflows/update.yml)

Повна історія офіційних курсів Національного банку України для всіх валют — статичними JSON і CSV файлами.
Оновлюється щодня автоматично. Без ключів, без лімітів, з CORS — можна читати прямо з браузера.

**Базова адреса:** `https://enjoyfx.github.io/uarates_data/v1/`

## Файли

| Файл | Що всередині |
|---|---|
| [`index.json`](https://enjoyfx.github.io/uarates_data/v1/index.json) | Каталог валют: коди, назви, перша й остання дата, чи котирується зараз |
| [`latest.json`](https://enjoyfx.github.io/uarates_data/v1/latest.json) | Останній встановлений курс кожної валюти, яку НБУ котирує зараз |
| [`USD.json`](https://enjoyfx.github.io/uarates_data/v1/USD.json) | Уся історія курсу валюти (будь-який код з каталогу: `EUR.json`, `PLN.json`, …) |
| [`USD.csv`](https://enjoyfx.github.io/uarates_data/v1/USD.csv) | Те саме в CSV — для Excel, Google Sheets, pandas |
| [`schema/`](https://enjoyfx.github.io/uarates_data/v1/schema/currency.schema.json) | JSON Schema для `currency`, `index`, `latest` |

### Запис курсу

```json
{"date": "2025-01-02", "rate": 2.6593, "units": 10, "rate_per_unit": 0.26593}
```

| Поле | Значення |
|---|---|
| `date` | Дата, на яку встановлено курс (ISO 8601) |
| `rate` | Офіційний курс: гривень за `units` одиниць валюти — саме так, як публікує НБУ |
| `units` | Кількість одиниць, за яку котирується валюта (1, 10, 100, 1000) |
| `rate_per_unit` | Гривень за 1 одиницю — по ньому зручно будувати графіки й рахувати зміни |

## Що варто знати

- **Історія починається з 02.09.1996** — дня введення гривні. Раніше НБУ встановлював курси в карбованцях, їх тут немає.
- **Кількість одиниць котирування змінювалась.** Наприклад, 28.12.2019 USD і EUR перейшли з котирування за 100 на за 1, JPY — з 1000 на 10.
  Тому `units` є в кожному записі. Для аналізу довгих періодів використовуйте `rate_per_unit`.
- **Кожен календарний день.** На вихідні й свята діє курс останнього робочого дня — так само, як у НБУ.
- **Курс наперед.** НБУ встановлює курс на наступний робочий день, тож остання дата у файлах зазвичай завтрашня або понеділок.
- **Оновлення** — щодня близько 16:17 (зима) / 17:17 (літо) за Києвом, плюс страхувальний запуск о 19:47 / 20:47. GitHub може запускати їх із запізненням до кількох годин. Курси за останні 14 днів щоразу перечитуються, тож рідкісні виправлення НБУ теж потрапляють у дані.
- Валюти, які НБУ перестав котирувати, лишаються у даних з `"quoted": false` в каталозі.
- **Версія** — частина адреси (`/v1/`). Несумісні зміни вийдуть як `/v2/`, а `/v1/` продовжить працювати.

## Приклади

**curl + jq** — курс USD на дату:
```bash
curl -s https://enjoyfx.github.io/uarates_data/v1/USD.json \
  | jq '.rates[] | select(.date == "2025-01-02")'
```

**JavaScript** — курси за період:
```js
const { rates } = await fetch("https://enjoyfx.github.io/uarates_data/v1/EUR.json").then(r => r.json());
const q1 = rates.filter(r => r.date >= "2025-01-01" && r.date <= "2025-03-31");
```

**Python (pandas)**:
```python
import pandas as pd
usd = pd.read_csv("https://enjoyfx.github.io/uarates_data/v1/USD.csv", parse_dates=["date"])
```

**Google Sheets**:
```
=IMPORTDATA("https://enjoyfx.github.io/uarates_data/v1/USD.csv")
```

## Як це працює

[`export.py`](export.py) щодня робить **один запит** до API НБУ — курси всіх валют за останні 14 днів плюс встановлені наперед —
дописує нові дні, перезаписує лише файли, що змінились, і GitHub Actions публікує теку `data/` на GitHub Pages.
Повна історія завантажується лише для валюти, якої ще немає у даних, або вручну: workflow **update** → *Run workflow* → *full*.
Записані дні ніколи не видаляються; якщо НБУ поверне повну історію коротшою, ніж уже є, експорт зупиниться й дані не перезапише.

Локально:
```bash
uv sync
uv run python export.py          # оновлення: 1 запит; з нуля — ~1 хвилина
uv run python export.py --full   # перезавантажити всю історію
uv run pytest             # тести + перевірка даних за JSON Schema
```

Веб-інтерфейс, що використовує ці курси: [uarates_web](https://github.com/EnjoyFX/uarates_web).

Джерело даних — [Національний банк України](https://bank.gov.ua/ua/markets/exchangerates).
