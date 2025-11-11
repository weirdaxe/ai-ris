# new_iris_autoscraper.py
# New Iris — Streamlit news scraper + AutoScraper manager
# Drop-in alternative page that extends your existing app with AutoScraper support.

import os
import io
import json
import time
import hashlib
import datetime as dt
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin

import streamlit as st
import pandas as pd

# Networking / parsing
import requests
try:
    import cloudscraper  # Cloudflare bypass if needed
except Exception:
    cloudscraper = None
from bs4 import BeautifulSoup
import feedparser
from dateutil import parser as dateparser
from concurrent.futures import ThreadPoolExecutor, as_completed

# AutoScraper
try:
    from autoscraper import AutoScraper
except Exception:
    AutoScraper = None

# local utilities (use your utils.py for atomic write)
from utils import atomic_write_json, load_json_safe, ensure_file_exists, sanitize_site_name, config_paths_for_site

# ---------- Constants ----------
APP_TITLE = "New Iris (AutoScraper-enabled)"
DATA_DIR = "data"
CONFIGS_DIR = "configs"
LINKS_FILE = "links.json"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CONFIGS_DIR, exist_ok=True)
ensure_file_exists(LINKS_FILE)

TZ = ZoneInfo("Europe/London")  # inclusive day capping in London time
REQ_TIMEOUT = (10, 30)  # connect, read

MS_BLUE = "#216CA6"
MS_GRAY = "#7C8A97"
MS_DARK = "#000000"
MS_LIGHT = "#FFFFFF"

# Countries and FIPS sourcecountry codes for GDELT (kept from original)
COUNTRIES = [
    "Serbia", "Kazakhstan", "Uzbekistan", "Armenia", "Azerbaijan", "Romania",
    "Poland", "Czech", "Hungary", "Ukraine", "Albania", "Montenegro",
    "Macedonia", "Georgia", "Russia"
]
FIPS_BY_COUNTRY = {
    "Serbia": "RI", "Kazakhstan": "KZ", "Uzbekistan": "UZ", "Armenia": "AM",
    "Azerbaijan": "AJ", "Romania": "RO", "Poland": "PL", "Czech": "EZ",
    "Hungary": "HU", "Ukraine": "UP", "Albania": "AL", "Montenegro": "MJ",
    "Macedonia": "MK", "Georgia": "GG", "Russia": "RS",
}

GDELT_THEMES = [
    "EPU_ECONOMY", "POLITICAL_TURMOIL", "USPEC_POLITICS_GENERAL1", "EPU_POLICY"
]

# ---------- Small UI CSS ----------
def ms_css():
    st.markdown(
        f"""
        <style>
            .block-container {{ padding-top: 0rem !important; }}
            .new-iris-header {{ display:flex; align-items:center; gap:0.6rem; border-bottom: 2px solid {MS_GRAY}; padding-bottom: 0.4rem; margin-bottom: 0.8rem; }}
            .new-iris-title {{ font-size: 1.4rem; font-weight: 700; color: {MS_DARK}; }}
            .stButton>button {{ background:{MS_BLUE}; color:{MS_LIGHT}; border:0; border-radius:6px; }}
            .stDownloadButton>button {{ background:{MS_GRAY}; color:{MS_LIGHT}; border:0; border-radius:6px; }}
            .ms-chip {{ display:inline-block; padding:2px 8px; border-radius:12px; background:{MS_BLUE}; color:white; font-size:0.75rem; }}
            .article-card {{ border:1px solid {MS_GRAY}33; border-radius:8px; padding:10px; margin-bottom:8px; }}
            .article-card a {{ color:{MS_BLUE}; text-decoration:none; }}
            .article-card a:hover {{ text-decoration:underline; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

# ---------- Networking helpers ----------
def cloudsafe_session():
    if cloudscraper:
        try:
            return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
        except Exception:
            pass
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                      " Chrome/122.0 Safari/537.36"
    })
    return s

SESSION = cloudsafe_session()

def canonicalize_url(url: str) -> str:
    try:
        u = urlparse(url.strip())
        scheme = "https" if u.scheme in ("http", "https") else "https"
        netloc = u.netloc.lower()
        path = u.path or "/"
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                                  "utm_content", "gclid", "fbclid"}]
        query = urlencode(q)
        norm = urlunparse((scheme, netloc, path.rstrip("/") or "/", "", "", ""))
        if query:
            norm = norm + "?" + query
        return norm
    except Exception:
        return url

def parse_any_datetime(value) -> dt.datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, time.struct_time):
            return dt.datetime.fromtimestamp(time.mktime(value))
        return dateparser.parse(str(value))
    except Exception:
        return None

# ---------- HTML/RSS scraping (mostly reused) ----------
def discover_feeds(base_url: str) -> list[str]:
    candidates = []
    roots = [base_url]
    u = urlparse(base_url)
    if not u.scheme:
        roots = [f"https://{base_url}", f"http://{base_url}"]
    for root in roots:
        for path in ["/rss", "/feed", "/rss.xml", "/feed.xml", "/index.xml", "/atom.xml", "/feeds"]:
            candidates.append(root.rstrip("/") + path)
        try:
            resp = SESSION.get(root, timeout=REQ_TIMEOUT)
            if resp.ok:
                soup = BeautifulSoup(resp.text, "lxml")
                for link in soup.find_all("link", attrs={"rel": ["alternate", "ALTERNATE"]}):
                    t = (link.get("type") or "").lower()
                    if "rss" in t or "atom" in t or "xml" in t:
                        href = link.get("href")
                        if href:
                            if href.startswith("//"):
                                href = "https:" + href
                            elif href.startswith("/"):
                                href = root.rstrip("/") + href
                            candidates.append(href)
        except Exception:
            continue
    uniq = []
    seen = set()
    for c in candidates:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq

def scrape_rss(feed_url: str) -> list[dict]:
    try:
        fp = feedparser.parse(feed_url)
        items = []
        for e in fp.entries:
            pub = parse_any_datetime(getattr(e, "published", None) or getattr(e, "updated", None) or getattr(e, "pubDate", None) or getattr(e, "updated_parsed", None) or getattr(e, "published_parsed", None))
            link = getattr(e, "link", None)
            title = getattr(e, "title", None)
            if link and title:
                items.append({
                    "title": title.strip(),
                    "url": canonicalize_url(link),
                    "published_utc": pub,
                    "source": urlparse(feed_url).netloc,
                    "via": "rss"
                })
        return items
    except Exception:
        return []

META_TIME_KEYS = [
    ("meta", {"property": "article:published_time"}),
    ("meta", {"name": "pubdate"}),
    ("meta", {"itemprop": "datePublished"}),
    ("meta", {"name": "date"}),
    ("time", {"itemprop": "datePublished"}),
    ("time", {"datetime": True}),
]

def extract_article_date(html: str) -> dt.datetime | None:
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag, attrs in META_TIME_KEYS:
            el = soup.find(tag, attrs=attrs)
            if el:
                val = el.get("content") or el.get("datetime") or el.text
                dtm = parse_any_datetime(val)
                if dtm:
                    return dtm
        og = soup.find("meta", {"property": "og:updated_time"}) or soup.find("meta", {"property": "og:published_time"})
        if og:
            dtm = parse_any_datetime(og.get("content"))
            if dtm:
                return dtm
    except Exception:
        return None
    return None

def scrape_html_listing(base_url: str, max_links: int = 60) -> list[dict]:
    items = []
    roots = [base_url]
    u = urlparse(base_url)
    if not u.scheme:
        roots = [f"https://{base_url}", f"http://{base_url}"]
    try:
        resp = SESSION.get(roots[0], timeout=REQ_TIMEOUT)
        if not resp.ok:
            return items
        soup = BeautifulSoup(resp.text, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = (a.text or "").strip()
            if not text or len(text) < 4:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = roots[0].rstrip("/") + href
            if base_url in href or urlparse(href).netloc.endswith(urlparse(roots[0]).netloc):
                if any(seg in href.lower() for seg in ["/news", "/article", "/polit", "/biz", "/202", "/20"]):
                    links.append((text, canonicalize_url(href)))
        seen = set()
        clean = []
        for t, ulink in links:
            if ulink not in seen:
                seen.add(ulink)
                clean.append((t, ulink))
        clean = clean[:max_links]
        def fetch_date(url):
            try:
                r = SESSION.get(url, timeout=REQ_TIMEOUT)
                if r.ok:
                    return extract_article_date(r.text)
            except Exception:
                return None
            return None
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_date, u): (t, u) for t, u in clean}
            for fut in as_completed(futs):
                t, ulink = futs[fut]
                pub = None
                try:
                    pub = fut.result()
                except Exception:
                    pub = None
                items.append({
                    "title": t,
                    "url": ulink,
                    "published_utc": pub,
                    "source": urlparse(roots[0]).netloc,
                    "via": "html"
                })
    except Exception:
        return items
    return items

def scrape_site(base: str) -> list[dict]:
    feeds = discover_feeds(base)
    out = []
    for f in feeds[:6]:
        out.extend(scrape_rss(f))
    if not out:
        out.extend(scrape_html_listing(base))
    return out

# ---------- helpers ----------
def dedup_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        key = hashlib.sha256((r.get("title","").strip().lower() + "|" + r.get("url","")).encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

def within_day_range(dt_utc: dt.datetime, start_d: date, end_d: date) -> bool:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    local = dt_utc.astimezone(TZ)
    d = local.date()
    return (start_d <= d <= end_d)

def cap_by_date(rows: list[dict], start_d: date, end_d: date) -> list[dict]:
    capped = []
    for r in rows:
        pub = r.get("published_utc")
        if isinstance(pub, str):
            pub = parse_any_datetime(pub)
        if pub:
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=dt.timezone.utc)
            r["published_utc"] = pub.astimezone(dt.timezone.utc)
            if within_day_range(r["published_utc"], start_d, end_d):
                capped.append(r)
    return capped

# ---------- AutoScraper integration ----------
def load_structured_links() -> dict:
    """
    Load links.json and normalize to new structure:
    country -> list of {host, config (filename or None), listing_path (optional)}
    Backwards-compatibility: if links.json contains country->list[str], convert.
    """
    raw = load_json_safe(LINKS_FILE)
    out = {}
    for c in COUNTRIES:
        entries = raw.get(c)
        if not entries:
            out[c] = []
            continue
        if isinstance(entries, list) and all(isinstance(x, str) for x in entries):
            out[c] = [{"host": x, "config": None, "listing_path": None} for x in entries]
        else:
            # assume already in new shape; sanitize keys
            normalized = []
            for e in entries:
                if isinstance(e, dict):
                    host = e.get("host") or e.get("hostname") or ""
                    config = e.get("config")
                    listing_path = e.get("listing_path")
                    normalized.append({"host": host, "config": config, "listing_path": listing_path})
            out[c] = normalized
    return out

def save_structured_links(links_map: dict):
    # Persist atomically
    atomic_write_json(LINKS_FILE, links_map)

def try_autoscraper_extract(host: str, config_file: str, listing_path: str|None = None, max_items=200) -> list[dict]:
    """
    Use an AutoScraper config to extract candidate article links from a host.
    Returns list of dicts like {title, url, published_utc, source, via: 'autoscraper'}.
    Strategy:
      - Load AutoScraper from configs/<config_file>
      - Try listing candidates in this order: listing_path (if provided), https://host/, https://host + listing_path if listing_path relative
      - Call scraper.get_result_similar(page_url, grouped=False) to get list of matched values.
      - Convert strings that look like URLs into canonical URLs; if strings are titles, ignore (we need URLs) — but if some values are relative URLs, resolve them.
      - For each candidate url, fetch page and extract date/title.
    """
    results = []
    if AutoScraper is None:
        return results
    try:
        config_path = os.path.join(CONFIGS_DIR, config_file)
        if not os.path.exists(config_path):
            return results
        scraper = AutoScraper()
        # Load may raise if file not autoscraper format; guard
        try:
            scraper.load(config_path)
        except Exception:
            # try loading JSON and using get_result_similar might still not work
            return results

        candidate_urls = []
        tried = []
        roots = [f"https://{host}", f"http://{host}"]
        if listing_path:
            # normalize listing path
            if listing_path.startswith("http"):
                tried.append(listing_path)
            elif listing_path.startswith("/"):
                tried.extend([roots[0].rstrip("/") + listing_path, roots[1].rstrip("/") + listing_path])
            else:
                # relative path, try both roots
                tried.extend([roots[0].rstrip("/") + "/" + listing_path.lstrip("/"), roots[1].rstrip("/") + "/" + listing_path.lstrip("/")])
        tried.extend(roots)

        for page_url in tried:
            try:
                vals = scraper.get_result_similar(page_url, grouped=False)
            except Exception:
                vals = []
            if not vals:
                continue
            # Heuristics: pick strings that look like URLs or that contain '/'
            for v in vals:
                if not isinstance(v, str):
                    continue
                s = v.strip()
                # Skip very short non-URL strings
                if len(s) < 8:
                    continue
                # If it looks like URL, canonicalize
                if s.startswith("//"):
                    s = "https:" + s
                if s.startswith("http://") or s.startswith("https://"):
                    candidate_urls.append(canonicalize_url(s))
                elif "/" in s:
                    # might be relative
                    candidate_urls.append(canonicalize_url(urljoin(page_url, s)))
            if candidate_urls:
                break

        # dedupe preserve order
        seen = set()
        clean = []
        for u in candidate_urls:
            if u not in seen:
                seen.add(u)
                clean.append(u)
            if len(clean) >= max_items:
                break

        # Fetch each article and extract details concurrently
        def fetch_info(url):
            try:
                r = SESSION.get(url, timeout=REQ_TIMEOUT)
                if not r.ok:
                    return None
                pub = extract_article_date(r.text)
                # Title fallback to <title> tag
                soup = BeautifulSoup(r.text, "lxml")
                t = None
                if soup.title and soup.title.string:
                    t = soup.title.string.strip()
                # fallback to og:title or meta title
                if not t:
                    og = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "title"})
                    if og:
                        t = og.get("content") or og.get("value")
                return {"title": t, "url": canonicalize_url(url), "published_utc": pub, "source": host, "via": "autoscraper"}
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_info, u): u for u in clean}
            for fut in as_completed(futs):
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    results.append(res)
    except Exception:
        return []
    return results

# ---------- Load existing links mapping and configs ----------
@st.cache_data(show_spinner=False)
def get_links_map_cached():
    return load_structured_links()

# ---------- GDELT functions (trimmed, reused) ----------
def yyyymmddhhmmss(dt_utc: dt.datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    dt_utc = dt_utc.astimezone(dt.timezone.utc)
    return dt_utc.strftime("%Y%m%d%H%M%S")

def gdelt_query_base(fips_code: str) -> str:
    themes = " OR ".join([f"theme:{t}" for t in GDELT_THEMES])
    return f"sourcecountry:{fips_code} ({themes})"

def gdelt_artlist_rolling(
    fips_code: str,
    start_d: date,
    end_d: date,
    include_json_fields=True,
    max_per_call=250,
    progress_cb=None,
) -> list[dict]:
    q = gdelt_query_base(fips_code)
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    results = []
    start_dt_utc = dt.datetime.combine(start_d, dt.time(0, 0, 0, tzinfo=TZ)).astimezone(dt.timezone.utc)
    end_dt_utc = dt.datetime.combine(end_d, dt.time(23, 59, 59, tzinfo=TZ)).astimezone(dt.timezone.utc)
    cursor_end = end_dt_utc
    batch_idx = 0

    while True:
        batch_idx += 1
        params = {
            "query": q,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(max_per_call),
            "sort": "DateDesc",
            "startdatetime": yyyymmddhhmmss(start_dt_utc),
            "enddatetime": yyyymmddhhmmss(cursor_end),
        }
        try:
            r = SESSION.get(endpoint, params=params, timeout=REQ_TIMEOUT)
            if not r.ok:
                if progress_cb:
                    progress_cb({"event": "error", "message": f"HTTP {r.status_code}"})
                break
            data = r.json()
            arts = data.get("articles", [])
        except Exception as e:
            if progress_cb:
                progress_cb({"event": "error", "message": str(e)})
            break

        if progress_cb:
            progress_cb({"event": "batch", "batch": batch_idx, "fetched": len(arts), "total": len(results)})

        if not arts:
            break

        batch = []
        for a in arts:
            pub = parse_any_datetime(a.get("seendate")) or parse_any_datetime(a.get("published")) or parse_any_datetime(a.get("pubdate"))
            url = canonicalize_url(a.get("url", ""))
            if not url:
                continue
            row = {
                "title": (a.get("title") or "").strip(),
                "url": url,
                "published_utc": (pub.replace(tzinfo=dt.timezone.utc) if pub and pub.tzinfo is None else pub),
                "source": a.get("domain") or "",
                "via": "gdelt"
            }
            if include_json_fields:
                row["gdelt_raw"] = a
            batch.append(row)

        results.extend(batch)

        if len(arts) < max_per_call:
            break

        oldest = min([parse_any_datetime(a.get("seendate")) for a in arts if a.get("seendate")] or [None])
        if not oldest:
            break
        oldest = oldest.replace(tzinfo=dt.timezone.utc) if oldest.tzinfo is None else oldest.astimezone(dt.timezone.utc)
        if oldest <= start_dt_utc:
            break
        cursor_end = oldest - timedelta(seconds=1)

        if len(results) > 10000:
            if progress_cb:
                progress_cb({"event": "warn", "message": "Stopping after 10k articles for safety."})
            break

    results = cap_by_date(results, start_d, end_d)
    results = dedup_rows(results)
    if progress_cb:
        progress_cb({"event": "done", "total": len(results)})
    return results

# ---------- Persistence / export (copied) ----------
def to_json_bytes(rows: list[dict]) -> bytes:
    def _canon(o):
        if isinstance(o, dt.datetime):
            return o.astimezone(dt.timezone.utc).isoformat()
        raise TypeError
    return json.dumps(rows, default=_canon, ensure_ascii=False, indent=2).encode("utf-8")

def to_csv_bytes(rows: list[dict]) -> bytes:
    df = pd.DataFrame([
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "published_utc": (r.get("published_utc").astimezone(dt.timezone.utc).isoformat() if isinstance(r.get("published_utc"), dt.datetime) else r.get("published_utc")),
            "source": r.get("source"),
            "via": r.get("via")
        } for r in rows
    ])
    return df.to_csv(index=False).encode("utf-8")

def save_cache(rows: list[dict], country: str, start_d: date, end_d: date):
    key = f"{country}_{start_d.isoformat()}_{end_d.isoformat()}"
    with open(os.path.join(DATA_DIR, f"{key}.json"), "wb") as f:
        f.write(to_json_bytes(rows))
    with open(os.path.join(DATA_DIR, f"{key}.csv"), "wb") as f:
        f.write(to_csv_bytes(rows))

# ---------- UI setup ----------
st.set_page_config(page_title=APP_TITLE, page_icon="logo.png", layout="wide")
ms_css()

with st.sidebar:
    brand_c1, brand_c2 = st.columns([0.22, 0.78])
    with brand_c1:
        try:
            st.image("logo.png", use_container_width=True)
        except Exception:
            st.write("")
    with brand_c2:
        st.markdown(f'<div class="new-iris-title">{APP_TITLE}</div>', unsafe_allow_html=True)
    st.markdown(f'<div style="border-bottom:2px solid {MS_GRAY}; margin:0.2rem 0 0.8rem 0;"></div>', unsafe_allow_html=True)

st.sidebar.subheader("Controls")
country = st.sidebar.selectbox("Country", COUNTRIES, index=0)

today_local = dt.datetime.now(TZ).date()
default_start = today_local - timedelta(days=1)
start_date, end_date = st.sidebar.date_input(
    "Date range (inclusive, Europe/London)",
    value=(default_start, today_local),
    min_value=today_local - timedelta(days=365),
    max_value=today_local
)

mode_choice = st.sidebar.radio("Data sources", options=["Local sources only", "GDELT only", "Local + GDELT"], index=2)

# Load structured links
links_map = get_links_map_cached()
current_entries = links_map.get(country, [])

st.sidebar.caption("Local sources for selected country")
# show hosts as editable text area, but we will also allow structured edits below
src_text = st.sidebar.text_area(
    "Hostnames (comma or newline separated) — quick edit (will overwrite structured list if saved)",
    value="\n".join([entry.get("host","") for entry in current_entries]),
    height=120,
    help="Examples: example.com, news.site.tld"
)
user_sources = sorted({s.strip().replace("https://", "").replace("http://", "").strip("/") for chunk in src_text.split("\n") for s in chunk.split(",") if s.strip()})

view = st.sidebar.selectbox("View", options=["User view", "Dev tools"], index=0)

scrape_btn = st.sidebar.button("Scrape now")

dl_placeholder = st.container()
feed_placeholder = st.container()
charts_placeholder = st.container()

# ---------- Scraping runner updated to use AutoScraper when assigned ----------
def run_scrape():
    rows_local, rows_gdelt = [], []
    structured = load_structured_links()

    # Build the host list (from quick text area OR structured)
    # We'll prefer the structured list for host entries if present
    hosts_entries = structured.get(country) or []
    if not hosts_entries:
        # fallback to quick list
        hosts_entries = [{"host": h, "config": None, "listing_path": None} for h in user_sources]

    # Local sources - we will iterate hosts_entries and for each host either run autoscraper if config exists or fallback scrape_site
    all_local = []
    for ent in hosts_entries:
        host = ent.get("host")
        cfg = ent.get("config")
        listing_path = ent.get("listing_path")
        if not host:
            continue
        st.write(f"→ scraping host: {host} {'(using AutoScraper: '+cfg+')' if cfg else ''}")
        try:
            if cfg:
                rows = try_autoscraper_extract(host, cfg, listing_path)
                # If autoscraper returned no rows, fallback
                if not rows:
                    rows = scrape_site(host)
            else:
                rows = scrape_site(host)
            rows = cap_by_date(dedup_rows(rows), start_date, end_date)
            all_local.extend(rows)
            st.write(f"   found {len(rows)} items")
        except Exception as e:
            st.write(f"   error scraping {host}: {e}")
    rows_local = all_local

    # GDELT
    if mode_choice in ("GDELT only", "Local + GDELT"):
        fips = FIPS_BY_COUNTRY.get(country)
        if not fips:
            st.error("No FIPS code mapping for selected country.")
        else:
            gdelt_box = st.status("Querying GDELT…", expanded=True)
            prog = st.progress(0, text="Starting GDELT…")
            total_seen = 0

            def _cb(ev: dict):
                nonlocal total_seen
                if ev.get("event") == "batch":
                    total_seen = ev.get("total", 0) + ev.get("fetched", 0)
                    gdelt_box.write(f"Batch {ev.get('batch')}: fetched {ev.get('fetched')} | cumulative ~{total_seen}")
                    prog.progress(min(0.99, (ev.get("batch", 1) % 10) / 10), text=f"GDELT batches processed: {ev.get('batch')}")
                elif ev.get("event") == "warn":
                    gdelt_box.write(f"Warning: {ev.get('message')}")
                elif ev.get("event") == "error":
                    gdelt_box.write(f"Error: {ev.get('message')}")
                elif ev.get("event") == "done":
                    gdelt_box.write(f"Done. Total GDELT articles after capping/dedup: {ev.get('total')}")
                    prog.progress(1.0, text="GDELT complete")

            rows_gdelt = gdelt_artlist_rolling(
                fips, start_date, end_date, include_json_fields=False, max_per_call=250, progress_cb=_cb
            )
            gdelt_box.update(label="GDELT complete", state="complete")
            prog.empty()
    else:
        rows_gdelt = []

    rows_all = dedup_rows(rows_local + rows_gdelt)
    save_cache(rows_all, country, start_date, end_date)
    return rows_all, rows_local, rows_gdelt

# ---------- Small helpers for UI ----------
def copy_to_clipboard_button(label: str, text: str):
    b64 = text.encode("utf-8").decode("utf-8")
    html = f"""
    <button onclick="navigator.clipboard.writeText(document.getElementById('copytarget').textContent)"
            style="padding:6px 10px;border-radius:6px;border:0;background:{MS_BLUE};color:white;cursor:pointer;">
        {label}
    </button>
    <pre id="copytarget" style="display:none;">{b64}</pre>
    """
    st.markdown(html, unsafe_allow_html=True)

def render_downloads(rows: list[dict]):
    json_bytes = to_json_bytes(rows)
    csv_bytes = to_csv_bytes(rows)
    fname_base = f"{country}_{start_date}_{end_date}"
    with dl_placeholder:
        c1, c2, c3 = st.columns([0.22,0.22,0.56])
        with c1:
            st.download_button("Download JSON", data=json_bytes, file_name=f"{fname_base}.json", mime="application/json")
        with c2:
            st.download_button("Download CSV", data=csv_bytes, file_name=f"{fname_base}.csv", mime="text/csv")
        with c3:
            copy_to_clipboard_button("Copy JSON to clipboard", json_bytes.decode("utf-8"))

def render_feed(rows_all, rows_local, rows_gdelt):
    with feed_placeholder:
        st.subheader("Articles")
        st.caption(f"Total: {len(rows_all)}  •  Local: {len(rows_local)}  •  GDELT: {len(rows_gdelt)}")
        rows_sorted = sorted(rows_all, key=lambda r: r.get("published_utc") or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        for r in rows_sorted:
            pub = r.get("published_utc")
            pub_s = ""
            if isinstance(pub, dt.datetime):
                pub_s = pub.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
            st.markdown(
                f"""
                <div class="article-card">
                    <div style="display:flex;justify-content:space-between;">
                        <div><span class="ms-chip">{r.get("via","")}</span></div>
                        <div style="color:{MS_GRAY};font-size:0.85rem;">{pub_s}</div>
                    </div>
                    <div style="margin-top:6px;font-weight:600;"><a href="{r.get("url")}" target="_blank">{r.get("title")}</a></div>
                    <div style="color:{MS_GRAY};font-size:0.85rem;">{r.get("source","")}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

def render_charts(fips: str):
    with charts_placeholder:
        st.subheader("GDELT timelines")
        col1, col2 = st.columns(2)
        with col1:
            df_vol = gdelt_timeline_csv("TimelineVol", fips, start_date, end_date, smooth=7)
            if not df_vol.empty:
                df_vol = df_vol.sort_values("datetime")
                st.line_chart(df_vol.set_index("datetime")["value"], height=220, use_container_width=True)
                st.caption("Volume of matching coverage")
            else:
                st.info("No volume data.")
        with col2:
            df_tone = gdelt_timeline_csv("TimelineTone", fips, start_date, end_date, smooth=7)
            if not df_tone.empty:
                df_tone = df_tone.sort_values("datetime")
                st.line_chart(df_tone.set_index("datetime")["value"], height=220, use_container_width=True)
                st.caption("Average tone")
            else:
                st.info("No tone data.")

# ---------- Views ----------
if view == "Dev tools":
    st.subheader("Dev tools — AutoScraper manager")
    st.caption("Upload AutoScraper config files, assign them to hosts, and test them.")

    # show existing configs in configs/
    configs_on_disk = sorted([fn for fn in os.listdir(CONFIGS_DIR) if fn.lower().endswith(".json")])
    st.write("Configs on disk:", configs_on_disk)

    # Upload new config file
    st.markdown("**Upload new AutoScraper config (.json)**")
    uploaded = st.file_uploader("Upload autoscraper JSON", type=["json"], accept_multiple_files=False)
    if uploaded:
        save_name = uploaded.name
        path = os.path.join(CONFIGS_DIR, save_name)
        try:
            bytes_data = uploaded.getvalue()
            with open(path, "wb") as f:
                f.write(bytes_data)
            st.success(f"Saved config to {path}")
            configs_on_disk = sorted([fn for fn in os.listdir(CONFIGS_DIR) if fn.lower().endswith(".json")])
        except Exception as e:
            st.error(f"Failed to save uploaded config: {e}")

    # Show and edit structured links for current country
    st.markdown("**Structured hosts for this country**")
    struct = load_structured_links()
    entries = struct.get(country, [])
    st.write(f"Currently {len(entries)} hosts assigned to {country}.")
    # Present a simple editor for each entry
    edited = []
    for i, ent in enumerate(entries):
        st.markdown(f"**Host #{i+1}**")
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            host_val = st.text_input(f"Host (hostname)", value=ent.get("host",""), key=f"host_{country}_{i}")
        with c2:
            cfg_sel = st.selectbox(f"Assign config (optional)", options=["(none)"] + configs_on_disk, index=(configs_on_disk.index(ent["config"]) + 1 if ent.get("config") in configs_on_disk else 0), key=f"cfg_{country}_{i}")
            cfg_val = None if cfg_sel == "(none)" else cfg_sel
        with c3:
            lp = st.text_input(f"listing_path", value=ent.get("listing_path") or "", key=f"lp_{country}_{i}")
        test_col = st.columns([1,1])
        with test_col[0]:
            if st.button(f"Test AutoScraper on host #{i+1}", key=f"test_{country}_{i}"):
                if cfg_val and AutoScraper:
                    with st.spinner("Running AutoScraper test..."):
                        try:
                            out = try_autoscraper_extract(host_val, cfg_val, lp, max_items=30)
                            st.write(f"Found {len(out)} items (preview first 10):")
                            st.json(out[:10])
                        except Exception as e:
                            st.error(f"Test failed: {e}")
                else:
                    st.warning("No config assigned or AutoScraper not available.")
        with test_col[1]:
            if st.button(f"Remove host #{i+1}", key=f"remove_{country}_{i}"):
                # mark removal by skipping append to edited
                st.experimental_rerun()
        edited.append({"host": host_val.strip(), "config": cfg_val, "listing_path": lp.strip() or None})

    # Add a new host UI
    st.markdown("**Add a new host**")
    new_host = st.text_input("New host (hostname)", value="", key=f"newhost_{country}")
    new_cfg = st.selectbox("Assign config for new host (optional)", options=["(none)"] + configs_on_disk, key=f"newcfg_{country}")
    new_lp = st.text_input("Optional listing path (e.g. /news?page=1)", value="", key=f"newlp_{country}")
    if st.button("Add host"):
        cfg_val = None if new_cfg == "(none)" else new_cfg
        if new_host.strip():
            edited.append({"host": new_host.strip(), "config": cfg_val, "listing_path": new_lp.strip() or None})
            st.success("Added (will save on persist).")
        else:
            st.error("Enter a hostname first.")

    # Quick-overwrite text area: if user edited that, replace structured list
    if st.button("Overwrite structured hosts from quick text area"):
        quick_list = sorted({s.strip().replace("https://", "").replace("http://", "").strip("/") for chunk in src_text.split("\n") for s in chunk.split(",") if s.strip()})
        edited = [{"host": h, "config": None, "listing_path": None} for h in quick_list]
        st.success("Will overwrite with quick list on save.")

    # Save structured mapping
    if st.button("Save structured hosts for this country"):
        struct[country] = [e for e in edited if e.get("host")]
        try:
            save_structured_links(struct)
            st.success("Saved structured hosts to links.json")
        except Exception as e:
            st.error(f"Failed to save links.json: {e}")

    st.markdown("---")
    st.info("Dev tools: you can upload AutoScraper configs and assign them to hosts for each country. When scraping, assigned configs will be used first to extract article links; if they fail or return nothing, app falls back to RSS/HTML heuristics.")

else:
    # User view
    if scrape_btn:
        rows_all, rows_local, rows_gdelt = run_scrape()
        render_downloads(rows_all)
        render_feed(rows_all, rows_local, rows_gdelt)
        if mode_choice in ("GDELT only", "Local + GDELT"):
            fips = FIPS_BY_COUNTRY.get(country)
            if fips:
                render_charts(fips)
    else:
        st.info("Set options in the sidebar. Press 'Scrape now' to run.")
