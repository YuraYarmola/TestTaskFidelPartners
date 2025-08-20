# SERP Monitor — README (повна інструкція)

Моніторить видачу Google (топ-10/топ-30) за списком ключів, щоденно (або за вказаним розкладом), фіксує **нові домени**, визначає **тип сайту** (продукт / огляд / медіа / блог), витягує **контакти** (email, соцмережі, сторінки контактів), зберігає у **SQLite** + **CSV**, опційно пушить у **Google Sheets**.

---

# Зразок тестової таблиці Google Sheets з результатами
https://docs.google.com/spreadsheets/d/1NmjntRxvrQJnMitxpE9JMpo2uqMfH8d0w_iADcne63A/edit?usp=sharing



## Зміст

* [Архітектура](#архітектура)
* [Швидкий старт у Docker](#швидкий-старт-у-docker)
* [Запуск без Docker (локально)](#запуск-без-docker-локально)
* [Налаштування оточення (.env)](#налаштування-оточення-env)
* [Файл ключових слів](#файл-ключових-слів)
* [Планувальник (розклад)](#планувальник-розклад)
* [Google Sheets інтеграція (повна інструкція)](#google-sheets-інтеграція-повна-інструкція)
* [Де взяти SHEETS\_KEY (ID таблиці)](#де-взяти-sheets_key-id-таблиці)
* [Пояснення про ключі SERPER/SerpAPI](#пояснення-про-ключі-serperserpapi)
* [Структура даних і вивантаження](#структура-даних-і-вивантаження)
* [Команди, логи, тюнінг продуктивності](#команди-логи-тюнінг-продуктивності)
* [Troubleshooting (типові помилки)](#Troubleshooting-типові-помилки)

---

## Архітектура

* **SERP провайдери:**

  * Primary: **Serper.dev** (обгортка над Google Search).
  * Fallback: **SerpAPI** (опційно).
* **Збагачення доменів:** паралельне (ThreadPool) завантаження HTML, евристична класифікація, пошук контактів/соцмереж, сторінок типу `/contact`, `/about`.
* **Зберігання:**

  * SQLite: `serp.db`
  * CSV експорт: `exports/snapshot_YYYY-MM-DD.csv`, `exports/domains_YYYY-MM-DD.csv`
  * (опційно) **Google Sheets**
* **Розклад:** вбудований планувальник

  * `RUN_EVERY_SECONDS` (інтервал).
* **Конфігурація:** через `.env` / env vars.

---

## Швидкий старт у Docker

1. **Підготуйте файли**

```bash
cp .env.example .env
mkdir -p config data
cp config/keywords.txt.example config/keywords.txt
```

2. **Вкажіть ключ провайдера** у `.env` (мінімум `SERPER_API_KEY`).

3. **Запуск**

```bash
docker compose up -d --build
```

4. **Логи**

```bash
docker compose logs -f serp-monitor
```

**Файли/томи за замовчуванням**

* База й експорти лежать у корені)
* Ключові слова — `./keywords.txt`)

## Запуск без Docker (локально)

1. **Python залежності**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
```

2. **ENV**

* Створіть `.env` (або експортуйте змінні оточення в shell).
* Мінімум: `SERPER_API_KEY`.

3. **Одноразовий прогін**

```bash
python app/serp_monitor.py run --keywords config/keywords.txt --top 30 --gl ua --hl uk
```

4. **Демон (безкінечний режим з розкладом)**

```bash
python app/serp_monitor.py serve
```

---

## Налаштування оточення (.env)

Приклад `.env.example` (скорочено, адаптований під ваш `docker-compose.yml`):

```dotenv
# -------------------------------
# Scheduling
# -------------------------------
# Run interval in seconds (default 6h = 21600)
RUN_EVERY_SECONDS=21600

# -------------------------------
# Providers
# -------------------------------
# Primary: Serper.dev API key (https://serper.dev)
SERPER_API_KEY=your_serper_api_key_here

# Optional fallback: SerpAPI (https://serpapi.com)
# SERPAPI_API_KEY=your_serpapi_api_key_here

# -------------------------------
# Localization
# -------------------------------
# Google Search country (gl=geo location, e.g., ua=Ukraine, us=USA)
GL=ua
# Google Search UI language
HL=uk
# How many top results to fetch (default 30)
TOP_N=30

# -------------------------------
# HTTP / Performance
# -------------------------------
HTTP_USER_AGENT="Mozilla/5.0 (SERP-Monitor/2.0)"
HTTP_TIMEOUT=10
HTTP_DELAY=0.2
MAX_CONTACT_PAGES=3
MAX_WORKERS=10

# -------------------------------
# Storage & Exports
# -------------------------------
DB_PATH=./serp.db
EXPORT_DIR=./exports

# Push results to Google Sheets (1 = yes, 0 = no)
PUSH_TO_SHEETS=1
SHEETS_NAME="SERP Monitor Results"

# -------------------------------
# Google Sheets (only if PUSH_TO_SHEETS=1)
# -------------------------------
GOOGLE_SHEETS_CREDENTIALS_JSON=service_account.json
SHEETS_KEY=your_google_sheets_key_here
# -------------------------------
# Keywords
# -------------------------------
KEYWORDS_PATH=keywords.txt
```

---

## Файл ключових слів

`config/keywords.txt` (по одному ключу на рядок, UTF-8):

```
iphone 15 review
купити електросамокат
найкращий хостинг україна
```

Коментарі можна починати з `#` — вони ігноруються.

---

## Планувальник (розклад)

* **Інтервал:** задайте `RUN_EVERY_SECONDS` (наприклад, `21600` для запуску кожні 6 год).
* **CRON:** або замість інтервалу використайте `SCHEDULE_CRON`, наприклад:

  ```
  SCHEDULE_CRON=5 8 * * *
  ```

  Це означає — щоденно о **08:05**.

> Використовується внутрішній планувальник, ніякого системного crontab у контейнері не потрібно.

---

## Google Sheets інтеграція (повна інструкція)

> **Обов’язково:** у таблиці потрібно надати **доступ “Editor” сервісному акаунту** (e-mail з JSON-ключа). Без цього — `PermissionError`/403.

1. **Створіть проєкт у Google Cloud Console**
   [https://console.cloud.google.com/](https://console.cloud.google.com/) → створіть або оберіть проєкт.

2. **Увімкніть APIs**

   * `APIs & Services → Library`
   * Увімкніть **Google Sheets API**
   * (Рекомендовано також **Google Drive API**, щоб працювали операції з файлами/аркушами)

3. **Створіть Service Account**

   * `APIs & Services → Credentials → Create Credentials → Service account`
   * Дайте роль **Editor** (або мінімально потрібні для Sheets/Drive).

4. **Згенеруйте JSON-ключ**

   * У Service Account → `Keys → Add Key → Create new key → JSON`.
   * Збережіть файл як `config/service_account.json` (у Docker монтуємо `./config:/config`).

5. **Поділіться доступом до Google Sheet**

   * Відкрийте вашу таблицю (Google Sheets) у браузері.
   * Натисніть **Share**.
   * Додайте **email сервісного акаунта** (із JSON, поле `client_email`) як **Editor**.
   * Якщо таблиця у **Shared Drive** — додайте сервісний акаунт у Shared Drive з роллю **Content manager** або вище.

6. **Налаштуйте змінні оточення**
   У `.env`:

   ```dotenv
   PUSH_TO_SHEETS=1
   SHEETS_NAME="SERP Monitor Results"     # Назва (якщо відкриваєте по імені)
   SHEETS_KEY=1AbCdEfGh...               # РЕКОМЕНДОВАНО: відкривати по ключу (ID з URL)
   GOOGLE_SHEETS_CREDENTIALS_JSON=/config/service_account.json
   ```

   Рекомендуємо вказати **`SHEETS_KEY`** і відкривати таблицю за ключем (надійніше, ніж за назвою).

7. **Перевірте в контейнері** (опційно) який email у сервісного акаунта:

   ```bash
   docker exec -it serp-monitor python -c "import os,json;print(json.load(open(os.getenv('GOOGLE_SHEETS_CREDENTIALS_JSON')))['client_email'])"
   ```

   Переконайтеся, що саме **цей** email додано в “Share” таблиці як Editor.

---

## Де взяти SHEETS\_KEY (ID таблиці)

Відкрийте Google Sheets у браузері. У адресному рядку:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIJKLmNOPqrstuVWxyz1234567890/edit#gid=0
                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                  Оце і є SHEETS_KEY (ID)
```

Скопіюйте цей фрагмент і вставте у `.env` як `SHEETS_KEY=...`.

---

## Пояснення про ключі SERPER/SerpAPI

* `SERPER_API_KEY` — ключ до **Serper.dev** (дешевше, простіше).
* `SERPAPI_API_KEY` — ключ до **SerpAPI** (дорожче, але більше фіч).

Код спершу намагається використати **Serper.dev**, і, якщо він недоступний/немає ключа, спробує **SerpAPI** як fallback (якщо задано `SERPAPI_API_KEY`). Обидва повертають **Google SERP** у JSON.

---

## Структура даних і вивантаження

* **SQLite**: `serp.db`

  * Таблиці: `serp_snapshot`, `keyword_domain`, `domain_status`.
* **CSV експорт** (на кожен запуск/день):

  * `exports/snapshot_YYYY-MM-DD.csv` — рядки SERP (позиція, URL, домен, топ-10/топ-30, чи новий домен).
  * `exports/domains_YYYY-MM-DD.csv` — довідник доменів (homepage, перша/остання поява, тип, контакти).

---

## Команди, логи, тюнінг продуктивності

**Запуск одноразово (локально):**

```bash
python app/serp_monitor.py run --keywords config/keywords.txt --top 30 --gl ua --hl uk
```

**Демон (Docker за замовчуванням):**

```bash
docker compose up -d --build
docker compose logs -f serp-monitor
```

**Налаштування продуктивності (через .env):**

* `MAX_WORKERS` — кількість потоків enrichment доменів (типово 8–10).
* `HTTP_TIMEOUT` — таймаут HTTP запитів.
* `HTTP_DELAY` — пауза між SERP-запитами (0.2–1.0 с).
* `TOP_N` — обсяг вибірки (10 або 30 рекомендовано).

---

## Troubleshooting (типові помилки)

### 1) `gspread.exceptions.APIError: [403]: The user's Drive storage quota has been exceeded.`

* Закінчилося місце у Google Drive облікового запису, **до якого належить таблиця**, або не надано сервісному акаунту доступ до Sheet.
* Дії:

  * Очистіть місце: [https://drive.google.com/drive/quota](https://drive.google.com/drive/quota) (видаліть великі файли, очистіть корзину).
  * Або заведіть іншу таблицю на акаунті з вільним місцем і **поділіться з сервісним акаунтом**.
  * Тимчасово вимкніть пуш у Sheets: `PUSH_TO_SHEETS=0`.

### 2) `PermissionError` / `403` при `open_by_key`

* Сервісний акаунт **не має прав** до таблиці.
* Додайте e-mail із `service_account.json` у **Share** як **Editor**.
* У Shared Drive — додайте SA до простору з роллю Content manager/вище.
* Перевірте правильність `SHEETS_KEY`.

### 3) Немає результатів SERP / помилки провайдера

* Перевірте `SERPER_API_KEY` або ввімкніть `SERPAPI_API_KEY`.
* Понизьте `TOP_N` або збільшіть `HTTP_DELAY` при лімітах/429.

### 4) CSV/DB не оновлюються

* Перевірте логи `docker compose logs -f serp-monitor`.
* Упевніться, що маунт `./data:/data` і шляхи `DB_PATH=/data/serp.db`, `EXPORT_DIR=/data/exports`.
