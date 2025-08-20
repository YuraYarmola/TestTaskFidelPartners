#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SERP Monitor — daily top-10/top-30 tracker with new-domain detection,
site-type classification, and contact extraction.

Features:
- Docker-friendly; can run as a long-lived service
- Built-in scheduler (cron-style via SCHEDULE_CRON or interval via RUN_EVERY_SECONDS)
- Concurrency for enrichment (ThreadPool) and pooled HTTP sessions
- Config via env vars
- CSV exports + optional Google Sheets sync
- Pluggable SERP providers: serper.dev (primary) and SerpAPI (fallback)

Run modes:
- One-off: python serp_monitor.py run --keywords /path/to/keywords.txt --top 30 --gl ua --hl uk
- Daemon (Docker): python serp_monitor.py serve
"""
import logging
import os
import re
import csv
import json
import time
import signal
import argparse
import sqlite3
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import tldextract
# Optional Google Sheets sync
try:
    import gspread
    from gspread.exceptions import APIError
    from oauth2client.service_account import ServiceAccountCredentials
    HAS_GSHEETS = True
except Exception as e:
    logging.error(e)
    HAS_GSHEETS = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", filename="logs/serp_monitor.log")

# ----------------------------- Configuration ----------------------------- #
DB_PATH = os.getenv("DB_PATH", "serp.db")
EXPORT_DIR = os.getenv("EXPORT_DIR", "exports")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Scheduling: choose either CRON or INTERVAL (seconds)
SCHEDULE_CRON = os.getenv("SCHEDULE_CRON")  # e.g. "5 8 * * *" daily 08:05
RUN_EVERY_SECONDS = int(os.getenv("RUN_EVERY_SECONDS", "86400"))  # default daily

# Keywords file
KEYWORDS_PATH = os.getenv("KEYWORDS_PATH", "keywords.txt")

# Providers
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
GL_DEFAULT = os.getenv("GL", "ua")
HL_DEFAULT = os.getenv("HL", "uk")
TOP_N_DEFAULT = int(os.getenv("TOP_N", "30"))

# HTTP
USER_AGENT = os.getenv("HTTP_USER_AGENT", "Mozilla/5.0 (SERP-Monitor/2.0)")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
HTTP_DELAY = float(os.getenv("HTTP_DELAY", "0.2"))
MAX_CONTACT_PAGES = int(os.getenv("MAX_CONTACT_PAGES", "3"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))  # enrichment concurrency

SESSION: Optional[requests.Session] = None

def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

# Patterns
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CONTACT_LINK_HINTS = [
    "contact", "contacts", "contact-us", "support", "feedback", "about", "about-us",
    "kontakt", "kontakty", "impressum", "контакт", "контакти", "про-нас", "о-нас"
]
SITE_TYPE_HINTS = {
    "product": ["add to cart", "buy now", "checkout", "/product/", "/products/", "schema.org/Product", "товар"],
    "review": ["review", "reviews", "rating", "рейтинг", "обзор", "порівняння", "best"],
    "media": ["news", "новини", "NewsArticle", "schema.org/NewsArticle"],
    "blog": ["blog", "/blog/", "BlogPosting", "schema.org/Article"],
}

@dataclass
class SERPItem:
    position: int
    title: str
    url: str
    domain: str
    snippet: str = ""

# ----------------------------- Utilities ----------------------------- #
def log(level: str, msg: str):
    levels = ["DEBUG", "INFO", "WARN", "ERROR"]
    if levels.index(level) >= levels.index(LOG_LEVEL):
        print(f"[{level}] {msg}", flush=True)

def ensure_dirs():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs("logs", exist_ok=True)

def today_str() -> str:
    return dt.date.today().isoformat()

def extract_domain(url: str) -> str:
    try:
        ext = tldextract.extract(url)
        return ".".join(p for p in [ext.domain, ext.suffix] if p)
    except Exception:
        return urlparse(url).netloc

def normalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        scheme = p.scheme or "https"
        netloc = (p.netloc or '').lower()
        path = p.path or "/"
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return url

# ----------------------------- DB Layer ----------------------------- #
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS serp_snapshot (
              id INTEGER PRIMARY KEY,
              snapshot_date TEXT NOT NULL,
              keyword TEXT NOT NULL,
              position INTEGER NOT NULL,
              url TEXT NOT NULL,
              title TEXT,
              domain TEXT NOT NULL,
              snippet TEXT,
              engine TEXT DEFAULT 'google',
              created_at TEXT DEFAULT (datetime('now')),
              UNIQUE(snapshot_date, keyword, position)
            );
            CREATE TABLE IF NOT EXISTS domain_status (
              domain TEXT PRIMARY KEY,
              homepage TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              site_type TEXT,
              contacts_json TEXT
            );
            CREATE TABLE IF NOT EXISTS keyword_domain (
              keyword TEXT NOT NULL,
              domain TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              PRIMARY KEY(keyword, domain)
            );
            """
        )
        conn.commit()

def upsert_snapshot(date_s: str, keyword: str, items: List[SERPItem]) -> None:
    with db() as conn:
        cur = conn.cursor()
        for it in items:
            cur.execute(
                """
                INSERT OR IGNORE INTO serp_snapshot
                (snapshot_date, keyword, position, url, title, domain, snippet)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date_s, keyword, it.position, normalize_url(it.url), it.title, it.domain, it.snippet)
            )
            cur.execute("SELECT 1 FROM keyword_domain WHERE keyword=? AND domain=?", (keyword, it.domain))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO keyword_domain(keyword, domain, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                    (keyword, it.domain, date_s, date_s)
                )
            else:
                cur.execute(
                    "UPDATE keyword_domain SET last_seen=? WHERE keyword=? AND domain=?",
                    (date_s, keyword, it.domain)
                )
        conn.commit()

def mark_domain_seen(domain: str, homepage: Optional[str], date_s: str) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT domain FROM domain_status WHERE domain=?", (domain,))
        if cur.fetchone():
            cur.execute("UPDATE domain_status SET last_seen=?, homepage=COALESCE(homepage, ?) WHERE domain=?",
                        (date_s, homepage, domain))
        else:
            cur.execute("INSERT INTO domain_status(domain, homepage, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                        (domain, homepage, date_s, date_s))
        conn.commit()

def update_domain_info(domain: str, site_type: Optional[str], contacts: Dict[str, Any]) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT contacts_json FROM domain_status WHERE domain=?", (domain,))
        row = cur.fetchone()
        merged = contacts
        if row and row["contacts_json"]:
            try:
                prev = json.loads(row["contacts_json"]) or {}
            except Exception:
                prev = {}
            for k in ["emails", "phones", "socials", "contact_pages"]:
                merged[k] = sorted(set((prev.get(k) or []) + (contacts.get(k) or [])))
        cur.execute(
            "UPDATE domain_status SET site_type=COALESCE(?, site_type), contacts_json=? WHERE domain=?",
            (site_type, json.dumps(merged, ensure_ascii=False), domain)
        )
        conn.commit()

# ----------------------------- SERP Providers ----------------------------- #
def search_serper(session: requests.Session, keyword: str, num: int = 30, gl: str = GL_DEFAULT, hl: str = HL_DEFAULT) -> List[Dict[str, Any]]:
    if not SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY not set")
    endpoint = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": keyword, "num": min(num, 100), "gl": gl, "hl": hl}
    resp = session.post(endpoint, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("organic", [])[:num]

def search_serpapi(session: requests.Session, keyword: str, num: int = 30, gl: str = GL_DEFAULT, hl: str = HL_DEFAULT) -> List[Dict[str, Any]]:
    if not SERPAPI_API_KEY:
        return []
    params = {
        "engine": "google",
        "api_key": SERPAPI_API_KEY,
        "q": keyword,
        "num": min(num, 100),
        "hl": hl,
        "gl": gl,
    }
    resp = session.get("https://serpapi.com/search", params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    org = data.get("organic_results", [])
    out = []
    for r in org[:num]:
        out.append({"title": r.get("title"), "link": r.get("link"), "snippet": r.get("snippet"), "position": r.get("position")})
    return out

def get_serp(session: requests.Session, keyword: str, num: int = TOP_N_DEFAULT, gl: str = GL_DEFAULT, hl: str = HL_DEFAULT) -> List[SERPItem]:
    try:
        raw = search_serper(session, keyword, num=num, gl=gl, hl=hl)
    except Exception as e:
        log("WARN", f"serper failed: {e}; trying serpapi")
        raw = search_serpapi(session, keyword, num=num, gl=gl, hl=hl)

    items: List[SERPItem] = []
    for i, r in enumerate(raw, start=1):
        url = r.get("link") or r.get("url") or r.get("cacheUrl") or ""
        title = r.get("title") or r.get("titleHighlighted") or ""
        snip = r.get("snippet") or ""
        if not url:
            continue
        items.append(SERPItem(position=i, title=title, url=url, domain=extract_domain(url), snippet=snip))
    return items

# ----------------------------- Classification & Contacts ----------------------------- #
def fetch_html(session: requests.Session, url: str) -> Tuple[str, Optional[str]]:
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        ct = resp.headers.get("content-type", "")
        if resp.status_code >= 400:
            return "", None
        if "text/html" not in ct and "application/xhtml" not in ct:
            return "", None
        return resp.text, resp.url
    except Exception:
        return "", None

def guess_site_type(title: str, html: str, url: str) -> Optional[str]:
    title_l = (title or "").lower()
    html_l = (html or "").lower()
    url_l = (url or "").lower()
    scores = {k: 0 for k in SITE_TYPE_HINTS}
    for kind, hints in SITE_TYPE_HINTS.items():
        for h in hints:
            if h in html_l or h in title_l or h in url_l:
                scores[kind] += 1
    chosen = max(scores, key=lambda k: scores[k])
    return chosen if scores[chosen] > 0 else None

def extract_contacts_from_html(html: str) -> Dict[str, List[str]]:
    emails = set(EMAIL_REGEX.findall(html))
    phones = set()  # optional: add phone regex if needed
    socials = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if any(s in href for s in ["facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "t.me", "youtube.com"]):
                socials.add(href)
            if href.startswith("mailto:"):
                emails.add(href[7:])
    except Exception:
        pass
    return {"emails": sorted(emails), "phones": sorted(phones), "socials": sorted(socials)}

def find_contact_pages(base_url: str, html: str) -> List[str]:
    pages = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = (a.get_text() or "").lower()
            if any(h in href.lower() for h in CONTACT_LINK_HINTS) or any(h in text for h in CONTACT_LINK_HINTS):
                pages.append(urljoin(base_url, href))
    except Exception:
        pass
    try:
        p = urlparse(base_url)
        base = f"{p.scheme}://{p.netloc}"
        defaults = ["/contact", "/contacts", "/contact-us", "/about", "/about-us", "/impressum"]
        pages.extend(urljoin(base, d) for d in defaults)
    except Exception:
        pass
    seen, uniq = set(), []
    for u in pages:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:MAX_CONTACT_PAGES]

def enrich_one(domain: str, homepage_hint: Optional[str]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Return (homepage, site_type, contacts) for a domain"""
    session = SESSION or _build_session()
    homepage = homepage_hint or f"https://{domain}/"
    html, final_url = fetch_html(session, homepage)
    if not html and homepage.startswith("https://"):
        html, final_url = fetch_html(session, "http://" + domain + "/")
    if final_url:
        homepage = final_url
    site_type = guess_site_type("", html, homepage)
    contacts = extract_contacts_from_html(html)
    contact_pages = find_contact_pages(homepage, html)
    for u in contact_pages:
        p_html, _ = fetch_html(session, u)
        if not p_html:
            continue
        extra = extract_contacts_from_html(p_html)
        for k in ["emails", "phones", "socials"]:
            contacts[k] = sorted(set(contacts.get(k, []) + extra.get(k, [])))
    contacts["contact_pages"] = contact_pages
    return homepage, site_type, contacts

# ----------------------------- Exporters ----------------------------- #
def export_latest(date_s: Optional[str] = None) -> Tuple[str, str]:
    ensure_dirs()
    if date_s in (None, "today"):
        date_s = today_str()
    snap_csv = os.path.join(EXPORT_DIR, f"snapshot_{date_s}.csv")
    domains_csv = os.path.join(EXPORT_DIR, f"domains_{date_s}.csv")
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT snapshot_date, keyword, position, url, title, domain, snippet FROM serp_snapshot WHERE snapshot_date=? ORDER BY keyword, position",
            (date_s,)
        )
        rows = cur.fetchall()
        with open(snap_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "keyword", "position", "url", "title", "domain", "snippet", "is_top10", "is_top30", "is_new_domain"])
            for r in rows:
                pos = int(r["position"])
                is_top10 = pos <= 10
                is_top30 = pos <= 30
                cur.execute("SELECT first_seen FROM keyword_domain WHERE keyword=? AND domain=?", (r["keyword"], r["domain"]))
                kd = cur.fetchone()
                is_new = kd and kd[0] == date_s
                w.writerow([r["snapshot_date"], r["keyword"], pos, r["url"], r["title"], r["domain"], r["snippet"], int(is_top10), int(is_top30), int(bool(is_new))])
        cur.execute("SELECT domain, homepage, first_seen, last_seen, site_type, contacts_json FROM domain_status")
        rows = cur.fetchall()
        with open(domains_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["domain", "homepage", "first_seen", "last_seen", "site_type", "emails", "phones", "socials", "contact_pages"])
            for r in rows:
                contacts = json.loads(r["contacts_json"]) if r["contacts_json"] else {}
                w.writerow([
                    r["domain"], r["homepage"], r["first_seen"], r["last_seen"], r["site_type"] or "",
                    ";".join(contacts.get("emails", [])),
                    ";".join(contacts.get("phones", [])),
                    ";".join(contacts.get("socials", [])),
                    ";".join(contacts.get("contact_pages", [])),
                ])
    return snap_csv, domains_csv

# ----------------------------- Google Sheets (optional) ----------------------------- #
def gsheets_push(date_s: str, spreadsheet_name: str = "SERP Monitor") -> None:
    if not HAS_GSHEETS:
        log("INFO", "gspread not installed, skipping Google Sheets push")
        return

    creds_json_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheets_key = os.getenv("SHEETS_KEY")  # <- ДОДАЙ ЦЕ В .env (ID існуючого файлу)
    if not creds_json_path or not os.path.exists(creds_json_path):
        log("INFO", "GOOGLE_SHEETS_CREDENTIALS_JSON missing, skipping Sheets push")
        return
    if not sheets_key:
        log("WARN", "SHEETS_KEY not set. Create a Sheet manually, share with Service Account, set SHEETS_KEY, then retry.")
        return

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_json_path, scope)
    gc = gspread.authorize(creds)

    try:
        sh = gc.open_by_key(sheets_key)  # ВІДКРИТИ ІСНУЮЧИЙ ФАЙЛ, НЕ СТВОРЮВАТИ
    except APIError as e:
        log("ERROR", f"Sheets open_by_key failed: {e}")
        return

    try:
        # --- Snapshot sheet (за датою)
        try:
            ws1 = sh.worksheet(date_s)
            ws1.clear()
        except Exception:
            ws1 = sh.add_worksheet(title=date_s, rows="1000", cols="10")

        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT snapshot_date, keyword, position, url, title, domain, snippet "
                "FROM serp_snapshot WHERE snapshot_date=? ORDER BY keyword, position",
                (date_s,)
            )
            rows = cur.fetchall()
            data = [["date", "keyword", "position", "url", "title", "domain", "snippet",
                     "is_top10", "is_top30", "is_new_domain"]]
            for r in rows:
                pos = int(r["position"])
                is_top10 = pos <= 10
                is_top30 = pos <= 30
                cur.execute("SELECT first_seen FROM keyword_domain WHERE keyword=? AND domain=?",
                            (r["keyword"], r["domain"]))
                kd = cur.fetchone()
                is_new = kd and kd[0] == date_s
                data.append([r["snapshot_date"], r["keyword"], pos, r["url"], r["title"],
                             r["domain"], r["snippet"], int(is_top10), int(is_top30), int(bool(is_new))])
        ws1.update(range_name="A1", values=data)


        # --- Domains sheet
        try:
            ws2 = sh.worksheet("domains")
            ws2.clear()
        except Exception:
            ws2 = sh.add_worksheet(title="domains", rows="1000", cols="10")

        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT domain, homepage, first_seen, last_seen, site_type, contacts_json FROM domain_status")
            rows = cur.fetchall()
            data = [["domain", "homepage", "first_seen", "last_seen", "site_type",
                     "emails", "phones", "socials", "contact_pages"]]
            for r in rows:
                contacts = json.loads(r["contacts_json"]) if r["contacts_json"] else {}
                data.append([
                    r["domain"], r["homepage"], r["first_seen"], r["last_seen"], r["site_type"] or "",
                    ";".join(contacts.get("emails", [])),
                    ";".join(contacts.get("phones", [])),
                    ";".join(contacts.get("socials", [])),
                    ";".join(contacts.get("contact_pages", [])),
                ])
        ws2.update(range_name="A1", values=data)

        log("INFO", "Sheets updated successfully.")
    except APIError as e:
        # Якщо саме проблема з квотою — лог і тихо скіп
        if "quota" in str(e).lower():
            log("ERROR", "Drive quota exceeded: disable PUSH_TO_SHEETS or free space / use another Sheet owner.")
        else:
            log("ERROR", f"Sheets update failed: {e}")
# ----------------------------- Core Run ----------------------------- #
def load_keywords(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith('#')]
    except FileNotFoundError:
        log("WARN", f"keywords file not found: {path}")
        return []

def run_once(keywords: List[str], top_n: int, gl: str, hl: str) -> None:
    global SESSION
    ensure_dirs()
    init_db()
    SESSION = SESSION or _build_session()
    date_s = today_str()

    # 1) Fetch SERPs per keyword
    for kw in keywords:
        log("INFO", f"Query: {kw}")
        items = get_serp(SESSION, kw, num=top_n, gl=gl, hl=hl)
        upsert_snapshot(date_s, kw, items)
        time.sleep(HTTP_DELAY)

    # 2) Enrich unique domains using thread pool
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT domain FROM serp_snapshot WHERE snapshot_date=?", (date_s,))
        domains = [r[0] for r in cur.fetchall()]

    tasks = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for d in domains:
            homepage_hint = f"https://{d}/"
            tasks[ex.submit(enrich_one, d, homepage_hint)] = d
        for fut in as_completed(tasks):
            d = tasks[fut]
            try:
                homepage, site_type, contacts = fut.result()
                mark_domain_seen(d, homepage, date_s)
                update_domain_info(d, site_type, contacts)
            except Exception as e:
                log("WARN", f"enrich failed for {d}: {e}")

    snap_csv, domains_csv = export_latest(date_s)
    log("INFO", f"Exported: {snap_csv} and {domains_csv}")

    if os.getenv("PUSH_TO_SHEETS", "0") in ("1", "true", "True"):
        gsheets_push(date_s, spreadsheet_name=os.getenv("SHEETS_NAME", "SERP Monitor"))

# ----------------------------- Scheduler/Daemon ----------------------------- #
_stop_flag = False

def _handle_signal(signum, frame):
    global _stop_flag
    log("INFO", f"Signal {signum} received, stopping after current cycle...")
    _stop_flag = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

def serve_loop():
    """Simple scheduler: supports either CRON string or fixed interval seconds."""
    keywords = load_keywords(KEYWORDS_PATH)
    if not keywords:
        log("ERROR", "No keywords loaded. Provide keywords.txt or set KEYWORDS_PATH")
        return

    if SCHEDULE_CRON:
        # minimal cron support (minute hour * * *). Recalculate next wake-up each minute.
        import croniter
        base = dt.datetime.now()
        itr = croniter.croniter(SCHEDULE_CRON, base)
        while not _stop_flag:
            next_run = itr.get_next(dt.datetime)
            wait = max(0, (next_run - dt.datetime.now()).total_seconds())
            log("INFO", f"Next run at {next_run.isoformat()} (in {int(wait)}s)")
            slept = 0
            while slept < wait and not _stop_flag:
                step = min(5, wait - slept)
                time.sleep(step)
                slept += step
            if _stop_flag:
                break
            run_once(keywords, TOP_N_DEFAULT, GL_DEFAULT, HL_DEFAULT)
    else:
        interval = max(60, RUN_EVERY_SECONDS)  # at least 1 minute
        log("INFO", f"Interval mode: every {interval}s")
        while not _stop_flag:
            start = dt.datetime.now()
            run_once(keywords, TOP_N_DEFAULT, GL_DEFAULT, HL_DEFAULT)
            if _stop_flag:
                break
            elapsed = (dt.datetime.now() - start).total_seconds()
            sleep_for = max(0, interval - elapsed)
            log("INFO", f"Sleeping {int(sleep_for)}s")
            slept = 0
            while slept < sleep_for and not _stop_flag:
                step = min(5, sleep_for - slept)
                time.sleep(step)
                slept += step

# ----------------------------- CLI ----------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="SERP Monitor (daemonized)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run once for provided keywords")
    p_run.add_argument("--keywords", help="Path to keywords file", default=KEYWORDS_PATH)
    p_run.add_argument("--top", type=int, default=TOP_N_DEFAULT)
    p_run.add_argument("--gl", default=GL_DEFAULT)
    p_run.add_argument("--hl", default=HL_DEFAULT)

    sub.add_parser("serve", help="Start the scheduler daemon")

    return p

def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "run":
        ks = load_keywords(args.keywords)
        run_once(ks, args.top, args.gl, args.hl)
    elif args.cmd == "serve":
        serve_loop()

if __name__ == "__main__":
    main()
