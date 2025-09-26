# New Iris — Streamlit news scraper + GDELT dashboard
# Notes:
# - Country FIPS codes for GDELT sourcecountry are hardcoded below (see citations in handoff message).
# - GDELT DOC 2.0 API used with rolling pagination to exceed 250 cap per query window. 
# - Morgan Stanley UI styling uses ms blue/gray; official logo usage guidance is black/white logo. 

import os
import io
import json
import time
import hashlib
import datetime as dt
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

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

# ---------- Constants ----------
APP_TITLE = "New Iris"
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

TZ = ZoneInfo("Europe/London")  # inclusive day capping in London time
REQ_TIMEOUT = (10, 30)  # connect, read

# Morgan Stanley color tokens (UI only; logo per official guide is black/white)
MS_BLUE = "#216CA6"    # ref palette
MS_GRAY = "#7C8A97"    # ref palette
MS_DARK = "#000000"
MS_LIGHT = "#FFFFFF"

# Countries and FIPS sourcecountry codes for GDELT
COUNTRIES = [
    "Serbia", "Kazakhstan", "Uzbekistan", "Armenia", "Azerbaijan", "Romania",
    "Poland", "Czech", "Hungary", "Ukraine", "Albania", "Montenegro",
    "Macedonia", "Georgia", "Russia"
]
FIPS_BY_COUNTRY = {
    "Serbia": "RI",        # FIPS change notice -> RI
    "Kazakhstan": "KZ",
    "Uzbekistan": "UZ",
    "Armenia": "AM",
    "Azerbaijan": "AJ",
    "Romania": "RO",
    "Poland": "PL",
    "Czech": "EZ",         # Czech Republic
    "Hungary": "HU",
    "Ukraine": "UP",
    "Albania": "AL",
    "Montenegro": "MJ",
    "Macedonia": "MK",     # North Macedonia
    "Georgia": "GG",
    "Russia": "RS",
}

# Default themes used for GDELT queries
GDELT_THEMES = [
    "EPU_ECONOMY", "POLITICAL_TURMOIL", "USPEC_POLITICS_GENERAL1", "EPU_POLICY"
]

# ---------- Utils ----------
def ms_css():
    st.markdown(
        f"""
        <style>
            .block-container {{
                padding-top: 0rem !important;
            }}
            header[data-testid="stHeader"] {{
            background: transparent;   /* remove white bar */
            box-shadow: none;          /* remove shadow line */}}

            div[data-testid="stSidebarCollapsedControl"] {{
            z-index: 10000;
            opacity: 1;
            pointer-events: auto;}}
            
            /* existing styles below … */
            .new-iris-header {{
                display:flex; align-items:center; gap:0.6rem;
                border-bottom: 2px solid {MS_GRAY};
                padding-bottom: 0.4rem; margin-bottom: 0.8rem;
            }}
            .new-iris-title {{ font-size: 1.4rem; font-weight: 700; color: {MS_DARK}; }}
            .stButton>button {{
                background:{MS_BLUE}; color:{MS_LIGHT}; border:0; border-radius:6px;
            }}
            .stDownloadButton>button {{ background:{MS_GRAY}; color:{MS_LIGHT}; border:0; border-radius:6px; }}
            .ms-chip {{
                display:inline-block; padding:2px 8px; border-radius:12px; background:{MS_BLUE}; color:white; font-size:0.75rem;
            }}
            .article-card {{
                border:1px solid {MS_GRAY}33; border-radius:8px; padding:10px; margin-bottom:8px;
            }}
            .article-card a {{ color:{MS_BLUE}; text-decoration:none; }}
            .article-card a:hover {{ text-decoration:underline; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

def cloudsafe_session():
    # Try cloudscraper first
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
        # Drop common tracking params
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
        # Accept dt, struct_time, or string
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, time.struct_time):
            return dt.datetime.fromtimestamp(time.mktime(value))
        return dateparser.parse(str(value))
    except Exception:
        return None

def within_day_range(dt_utc: dt.datetime, start_d: date, end_d: date) -> bool:
    # Interpret user range in Europe/London days inclusive; compare with UTC by converting dt_utc to London
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    local = dt_utc.astimezone(TZ)
    d = local.date()
    return (start_d <= d <= end_d)

def yyyymmddhhmmss(dt_utc: dt.datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    dt_utc = dt_utc.astimezone(dt.timezone.utc)
    return dt_utc.strftime("%Y%m%d%H%M%S")

@st.cache_data(show_spinner=False)
def load_links_json() -> dict:
    # Try ./links.json else create placeholder
    path = "links.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Normalize hostnames only (no scheme)
            norm = {k: sorted({urlparse(u).netloc or u for u in v}) for k, v in data.items()}
            return norm
        except Exception as e:
            st.warning(f"Failed to read links.json: {e}")
    # Placeholder
    placeholder = {c: [] for c in COUNTRIES}
    # Save a visible placeholder file for editing
    try:
        with open(os.path.join(DATA_DIR, "links.placeholder.json"), "w", encoding="utf-8") as f:
            json.dump(placeholder, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return placeholder

# ---------- RSS and HTML scraping ----------
def discover_feeds(base_url: str) -> list[str]:
    # Try common feed endpoints and HTML <link> discovery
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
    # unique
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
        # Try common meta tags
        for tag, attrs in META_TIME_KEYS:
            el = soup.find(tag, attrs=attrs)
            if el:
                val = el.get("content") or el.get("datetime") or el.text
                dtm = parse_any_datetime(val)
                if dtm:
                    return dtm
        # OpenGraph as fallback
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
            # Normalize relative to site
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = roots[0].rstrip("/") + href
            if base_url in href or urlparse(href).netloc.endswith(urlparse(roots[0]).netloc):
                # Heuristic: likely article paths
                if any(seg in href.lower() for seg in ["/news", "/article", "/polit", "/biz", "/202", "/20"]):
                    links.append((text, canonicalize_url(href)))
        # Dedup links preserving first title
        seen = set()
        clean = []
        for t, ulink in links:
            if ulink not in seen:
                seen.add(ulink)
                clean.append((t, ulink))
        clean = clean[:max_links]
        # Fetch pages concurrently for published time
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
    # RSS-first, then HTML
    # base is hostname or full URL
    feeds = discover_feeds(base)
    out = []
    for f in feeds[:6]:
        out.extend(scrape_rss(f))
    if not out:
        out.extend(scrape_html_listing(base))
    return out

# --- Progress helpers ---
def scrape_local_with_progress(hosts: list[str], start_d: date, end_d: date) -> list[dict]:
    """Scrape hosts sequentially with visible progress and per-site counts."""
    rows_local = []
    n = len([h for h in hosts if h])
    if n == 0:
        return rows_local

    prog = st.progress(0, text="Starting local scraping…")
    with st.status("Scraping local sources", expanded=True) as status:
        for i, host in enumerate([h for h in hosts if h], start=1):
            status.update(label=f"Scraping {host}  ({i}/{n})")
            status.write(f"→ {host}: starting…")
            try:
                items = scrape_site(host)
                items = cap_by_date(dedup_rows(items), start_d, end_d)
                rows_local.extend(items)
                status.write(f"✓ {host}: {len(items)} items")
            except Exception as e:
                status.write(f"× {host}: error {e}")
            prog.progress(i / n, text=f"Scraping {host}  ({i}/{n})")
        status.update(label="Local sources complete", state="complete")
    prog.empty()
    return rows_local



def dedup_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        key = hashlib.sha256((r.get("title","").strip().lower() + "|" + r.get("url","")).encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

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
        # If no date, drop (cannot ensure capping)
    return capped

# ---------- GDELT ----------
def gdelt_query_base(fips_code: str) -> str:
    # Build the query expression using GDELT themes and sourcecountry
    themes = " OR ".join([f"theme:{t}" for t in GDELT_THEMES])
    return f"sourcecountry:{fips_code} ({themes})"

def gdelt_artlist_rolling(
    fips_code: str,
    start_d: date,
    end_d: date,
    include_json_fields=True,
    max_per_call=250,
    progress_cb=None,  # <— added
) -> list[dict]:
    # Iterate backwards to include all hits across the window
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

def gdelt_timeline_csv(mode: str, fips_code: str, start_d: date, end_d: date, smooth=7) -> pd.DataFrame:
    # mode in {"TimelineVol", "TimelineTone"}
    q = gdelt_query_base(fips_code)
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    start_dt_utc = dt.datetime.combine(start_d, dt.time(0, 0, 0, tzinfo=TZ)).astimezone(dt.timezone.utc)
    end_dt_utc = dt.datetime.combine(end_d, dt.time(23, 59, 59, tzinfo=TZ)).astimezone(dt.timezone.utc)
    params = {
        "query": q,
        "mode": mode,
        "timespan": "1y",  # allow up to a year window if user selects long ranges; server will respect start/end if provided 
        "timelinesmooth": str(smooth),
        "timezoom": "yes",
        "startdatetime": yyyymmddhhmmss(start_dt_utc),
        "enddatetime": yyyymmddhhmmss(end_dt_utc),
        # CSV is default; we parse text below
    }
    r = SESSION.get(endpoint, params=params, timeout=REQ_TIMEOUT)
    if not r.ok or not r.text:
        return pd.DataFrame(columns=["datetime", "value"])
    lines = [ln.strip() for ln in r.text.strip().splitlines() if ln.strip()]
    # Expect header, then rows "YYYYMMDDHHMMSS,value"
    rows = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        dts, val = parts[0], parts[1]
        try:
            dtm = dt.datetime.strptime(dts, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
            rows.append({"datetime": dtm, "value": float(val)})
        except Exception:
            continue
    return pd.DataFrame(rows)

# ---------- Persistence / export ----------
def to_json_bytes(rows: list[dict]) -> bytes:
    # Convert datetimes to ISO
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

# ---------- UI ----------
st.set_page_config(page_title=APP_TITLE, page_icon="logo.png", layout="wide")
ms_css()

# Sidebar branding — logo + app name at very top
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

    
# Sidebar controls
st.sidebar.subheader("Controls")
country = st.sidebar.selectbox("Country", COUNTRIES, index=0)

# Date range (inclusive, days)
today_local = dt.datetime.now(TZ).date()
default_start = today_local - timedelta(days=1)
start_date, end_date = st.sidebar.date_input(
    "Date range (inclusive, Europe/London)",
    value=(default_start, today_local),
    min_value=today_local - timedelta(days=365),
    max_value=today_local
)

mode_choice = st.sidebar.radio("Data sources", options=["Local sources only", "GDELT only", "Local + GDELT"], index=2)

# Load local links and allow user to add
links_map = load_links_json()
current_sources = links_map.get(country, [])
st.sidebar.caption("Local sources for selected country")
src_text = st.sidebar.text_area(
    "Hostnames (comma or newline separated)",
    value="\n".join(current_sources),
    height=120,
    help="Examples: example.com, news.site.tld"
)
user_sources = sorted({s.strip().replace("https://", "").replace("http://", "").strip("/")
                       for chunk in src_text.split("\n") for s in chunk.split(",") if s.strip()})

# Dev view
view = st.sidebar.selectbox("View", options=["User view", "Dev tools"], index=0)

# Action buttons
scrape_btn = st.sidebar.button("Scrape now")

# Middle: download section placeholder
dl_placeholder = st.container()
feed_placeholder = st.container()
charts_placeholder = st.container()

def run_scrape():
    rows_local, rows_gdelt = [], []

    # Local sources
    if mode_choice in ("Local sources only", "Local + GDELT"):
        rows_local = scrape_local_with_progress(user_sources, start_date, end_date)

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
                    # progress is indeterminate; show pulsing text via modulo
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

    rows_all = dedup_rows(rows_local + rows_gdelt)
    save_cache(rows_all, country, start_date, end_date)
    return rows_all, rows_local, rows_gdelt

def copy_to_clipboard_button(label: str, text: str):
    # Injects a small JS button to copy JSON/CSV
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
        # Sort newest first
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
    st.subheader("Dev tools")
    st.caption("Use this to test a single source extractor quickly.")
    test_host = st.text_input("Hostname or URL to test", value=(user_sources[0] if user_sources else ""))
    if st.button("Test source"):
        with st.spinner("Testing extractor"):
            rows = scrape_site(test_host)
            rows = cap_by_date(dedup_rows(rows), start_date, end_date)
        st.write(f"Found {len(rows)} items")
        if rows:
            df = pd.DataFrame([{
                "title": r["title"],
                "url": r["url"],
                "published_utc": r["published_utc"].astimezone(dt.timezone.utc).isoformat() if isinstance(r["published_utc"], dt.datetime) else r["published_utc"],
                "via": r["via"],
                "source": r["source"],
            } for r in rows])
            st.dataframe(df, use_container_width=True, height=380)

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
