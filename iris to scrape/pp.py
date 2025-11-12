import streamlit as st
import os
import io
import json
import time
import hashlib
import datetime as dt
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import List, Optional
from autoscraper import AutoScraper

import pandas as pd
import requests
try:
    import cloudscraper
except Exception:
    cloudscraper = None
from bs4 import BeautifulSoup
import feedparser
from dateutil import parser as dateparser
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import scraper utilities
from scraper_utils import (
    build_scraper, get_grouped_results, test_scraper, 
    scrape_pages_collect_items, infer_field_mapping, assemble_items_from_grouped
)
from utils import (
    load_json_safe, ensure_file_exists, atomic_write_json,
    sanitize_site_name, config_paths_for_site
)

# ---------- Constants ----------
APP_TITLE = "News GPT Scrape"
DATA_DIR = "data"
CONFIGS_DIR = "configs"
LINKS_FILE = "links.json"
PAGINATION_FILE = "pagination.json"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CONFIGS_DIR, exist_ok=True)
ensure_file_exists(LINKS_FILE)
ensure_file_exists(PAGINATION_FILE)

TZ = ZoneInfo("Europe/London")
REQ_TIMEOUT = (10, 30)

# Morgan Stanley colors
MS_BLUE = "#216CA6"
MS_GRAY = "#7C8A97"
MS_DARK = "#000000"
MS_LIGHT = "#FFFFFF"

# Countries and FIPS codes
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

# ---------- Styling ----------
def ms_css():
    st.markdown(
        f"""
        <style>
            .block-container {{ padding-top: 0rem !important; }}
            header[data-testid="stHeader"] {{ background: transparent; box-shadow: none; }}
            div[data-testid="stSidebarCollapsedControl"] {{ z-index: 10000; opacity: 1; pointer-events: auto; }}
            
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
                display:inline-block; padding:2px 8px; border-radius:12px; 
                background:{MS_BLUE}; color:white; font-size:0.75rem;
            }}
            .article-card {{
                border:1px solid {MS_GRAY}33; border-radius:8px; padding:10px; margin-bottom:8px;
            }}
            .article-card a {{ color:{MS_BLUE}; text-decoration:none; }}
            .article-card a:hover {{ text-decoration:underline; }}
            .site-preview {{
                background:#f8f9fa; border-left:3px solid {MS_BLUE}; 
                padding:8px 12px; margin:8px 0; border-radius:4px;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

# ---------- Utils ----------
def cloudsafe_session():
    if cloudscraper:
        try:
            return cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        except Exception:
            pass
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0 Safari/537.36"
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
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", 
                                  "utm_term", "utm_content", "gclid", "fbclid"}]
        query = urlencode(q)
        norm = urlunparse((scheme, netloc, path.rstrip("/") or "/", "", "", ""))
        if query:
            norm = norm + "?" + query
        return norm
    except Exception:
        return url

def parse_any_datetime(value) -> Optional[dt.datetime]:
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

def within_day_range(dt_utc: dt.datetime, start_d: date, end_d: date) -> bool:
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

def dedup_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        key = hashlib.sha256(
            (r.get("title", "").strip().lower() + "|" + r.get("url", "")).encode("utf-8")
        ).hexdigest()
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
        else:
            # Include items without dates
            capped.append(r)
    return capped

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
            "published_utc": (
                r.get("published_utc").astimezone(dt.timezone.utc).isoformat() 
                if isinstance(r.get("published_utc"), dt.datetime) 
                else r.get("published_utc")
            ),
            "source": r.get("source"),
        } for r in rows
    ])
    return df.to_csv(index=False).encode("utf-8")

# ---------- Pagination Utils ----------
def load_pagination_config() -> dict:
    """Load pagination.json"""
    return load_json_safe(PAGINATION_FILE)

def save_pagination_config(pagination_data: dict):
    """Save pagination.json"""
    atomic_write_json(PAGINATION_FILE, pagination_data)

def get_pagination_for_config(config_filename: str) -> Optional[dict]:
    """Get pagination settings for a specific config file"""
    pagination = load_pagination_config()
    return pagination.get(config_filename)

def set_pagination_for_config(config_filename: str, pagination_settings: Optional[dict]):
    """Set pagination settings for a config file"""
    pagination = load_pagination_config()
    if pagination_settings is None:
        pagination.pop(config_filename, None)
    else:
        pagination[config_filename] = pagination_settings
    save_pagination_config(pagination)

# ---------- GDELT Functions ----------
def gdelt_query_base(fips_code: str) -> str:
    themes = " OR ".join([f"theme:{t}" for t in GDELT_THEMES])
    return f"sourcecountry:{fips_code} ({themes})"

def gdelt_artlist_rolling(
    fips_code: str, start_d: date, end_d: date,
    include_json_fields=False, max_per_call=250, progress_cb=None
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
            "query": q, "mode": "ArtList", "format": "json",
            "maxrecords": str(max_per_call), "sort": "DateDesc",
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
            progress_cb({"event": "batch", "batch": batch_idx, 
                        "fetched": len(arts), "total": len(results)})

        if not arts:
            break

        batch = []
        for a in arts:
            pub = (parse_any_datetime(a.get("seendate")) or 
                   parse_any_datetime(a.get("published")) or 
                   parse_any_datetime(a.get("pubdate")))
            url = canonicalize_url(a.get("url", ""))
            if not url:
                continue
            row = {
                "title": (a.get("title") or "").strip(),
                "url": url,
                "published_utc": (pub.replace(tzinfo=dt.timezone.utc) 
                                 if pub and pub.tzinfo is None else pub),
                "source": a.get("domain") or "",
            }
            if include_json_fields:
                row["gdelt_raw"] = a
            batch.append(row)

        results.extend(batch)

        if len(arts) < max_per_call:
            break

        oldest = min([parse_any_datetime(a.get("seendate")) 
                     for a in arts if a.get("seendate")] or [None])
        if not oldest:
            break
        oldest = (oldest.replace(tzinfo=dt.timezone.utc) 
                 if oldest.tzinfo is None else oldest.astimezone(dt.timezone.utc))
        if oldest <= start_dt_utc:
            break
        cursor_end = oldest - timedelta(seconds=1)

        if len(results) > 10000:
            if progress_cb:
                progress_cb({"event": "warn", "message": "Stopping after 10k articles"})
            break

    results = cap_by_date(results, start_d, end_d)
    results = dedup_rows(results)
    if progress_cb:
        progress_cb({"event": "done", "total": len(results)})
    return results

def gdelt_timeline_csv(mode: str, fips_code: str, start_d: date, end_d: date, smooth=7) -> pd.DataFrame:
    q = gdelt_query_base(fips_code)
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    start_dt_utc = dt.datetime.combine(start_d, dt.time(0, 0, 0, tzinfo=TZ)).astimezone(dt.timezone.utc)
    end_dt_utc = dt.datetime.combine(end_d, dt.time(23, 59, 59, tzinfo=TZ)).astimezone(dt.timezone.utc)
    params = {
        "query": q, "mode": mode, "timespan": "1y",
        "timelinesmooth": str(smooth), "timezoom": "yes",
        "startdatetime": yyyymmddhhmmss(start_dt_utc),
        "enddatetime": yyyymmddhhmmss(end_dt_utc),
    }
    r = SESSION.get(endpoint, params=params, timeout=REQ_TIMEOUT)
    if not r.ok or not r.text:
        return pd.DataFrame(columns=["datetime", "value"])
    lines = [ln.strip() for ln in r.text.strip().splitlines() if ln.strip()]
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

# ---------- AutoScraper Config Functions ----------
def load_configs_for_country(country: str) -> list[dict]:
    """Load all AutoScraper configs assigned to a country."""
    links = load_json_safe(LINKS_FILE)
    config_files = links.get(country, [])
    if isinstance(config_files, str):
        config_files = [config_files]
    
    configs = []
    for cfg_file in config_files:
        cfg_path = os.path.join(CONFIGS_DIR, cfg_file)
        if os.path.exists(cfg_path):
            cfg = load_json_safe(cfg_path)
            if cfg:
                cfg["_filename"] = cfg_file
                configs.append(cfg)
    
    return configs

def scrape_with_autoscraper_config(cfg: dict, start_d: date, end_d: date) -> list[dict]:
    """Scrape using saved AutoScraper config with pagination settings from pagination.json."""
    try:
        url = cfg.get("url", "")
        saved_rules = cfg.get("saved_from_rules", [])
        mapping = cfg.get("mapping", {})
        config_filename = cfg.get("_filename", "")
        scraper_path = cfg.get("scraper_file", "")
        
        if not scraper_path:
            return []
        
        scraper_full_path = os.path.join(CONFIGS_DIR, scraper_path)
        if not os.path.exists(scraper_full_path):
            return []
        
        scraper = AutoScraper()
        scraper.load(scraper_full_path)
        
        pagination = get_pagination_for_config(config_filename) if config_filename else None
        
        collected = []
        if pagination and pagination.get("page_url_template"):
            page_url_template = pagination.get("page_url_template", "")
            start_page = pagination.get("start_page", 1)
            max_pages = pagination.get("max_pages", 10)
            cutoff_date = pagination.get("cutoff_date", "")
            
            collected, _ = scrape_pages_collect_items(
                scraper, page_url_template,
                start_page=int(start_page),
                max_pages=int(max_pages),
                cutoff_date_iso=cutoff_date if cutoff_date else None,
                mapping=mapping,
                selected_rule_names=saved_rules
            )
        else:
            grouped = get_grouped_results(scraper, url)
            if saved_rules:
                grouped = {k: v for k, v in grouped.items() if k in saved_rules}
            collected = assemble_items_from_grouped(grouped, mapping)
        
        for item in collected:
            item["source"] = cfg.get("site_name", urlparse(url).netloc)
            if item.get("date") and isinstance(item.get("date"), str):
                try:
                    item["published_utc"] = dt.datetime.strptime(item["date"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
                except Exception:
                    pass
        
        return collected
        
    except Exception as e:
        return []

# ---------- Heavy Scrape Function ----------
def heavy_scrape_function(country: str, start_d=dt.datetime.now(TZ).date(), end_d=dt.datetime.now(TZ).date()- timedelta(days=1)) -> list[dict]:
    """
    Main scraping function for API endpoint.
    Scrapes AutoScraper configs only for the given country.
    Returns list of article dictionaries.
    """
    rows_auto = []
    
    # Load configs for country
    configs = load_configs_for_country(country)
    
    if not configs:
        return []
    
    # Scrape each config
    for cfg in configs:
        items = scrape_with_autoscraper_config(cfg, start_d, end_d)
        rows_auto.extend(items)
    
    # Deduplicate
    rows_auto = dedup_rows(rows_auto)
    
    return rows_auto

# ---------- Session State Init ----------
if "last_grouped" not in st.session_state:
    st.session_state.last_grouped = {}
if "last_scraper_present" not in st.session_state:
    st.session_state.last_scraper_present = False
if "last_autoscraper_obj" not in st.session_state:
    st.session_state.last_autoscraper_obj = None
if "collected_items" not in st.session_state:
    st.session_state.collected_items = []
if "confirmed_mapping" not in st.session_state:
    st.session_state.confirmed_mapping = None
if "confirmed_selected_rule_names" not in st.session_state:
    st.session_state.confirmed_selected_rule_names = None

# ========== ENDPOINT HANDLING ==========
# Check if this is an API call
params = st.experimental_get_query_params()

if params.get("action", [""])[0] == "scrape":
    import html
    import streamlit.components.v1 as components
    
    # Get parameters
    country_name = params.get("country", [""])[0]
    raw_mode = params.get("raw", ["0"])[0] in ("1", "true", "True")
    
    # Parse date range (default to yesterday if not provided)
    today_local = dt.datetime.now(TZ).date()
    default_start = today_local - timedelta(days=1)
    
    try:
        start_date_str = params.get("start_date", [""])[0]
        start_date = dt.datetime.strptime(start_date_str, "%Y-%m-%d").date() if start_date_str else default_start
    except Exception:
        start_date = default_start
    
    try:
        end_date_str = params.get("end_date", [""])[0]
        end_date = dt.datetime.strptime(end_date_str, "%Y-%m-%d").date() if end_date_str else today_local
    except Exception:
        end_date = today_local
    
    # Validate country
    if not country_name:
        st.json({"error": "Missing parameter: country"})
        st.stop()
    
    if country_name not in COUNTRIES:
        st.json({"error": f"Invalid country. Must be one of: {', '.join(COUNTRIES)}"})
        st.stop()
    
    # Execute scraping
    try:
        results = heavy_scrape_function(country_name, start_date, end_date)
        
        # Convert to JSON-serializable format
        def _canon(o):
            if isinstance(o, dt.datetime):
                return o.astimezone(dt.timezone.utc).isoformat()
            raise TypeError
        
        payload = {
            "country": country_name,
            "count": len(results),
            "results": results
        }
        
        json_str = results #json.dumps(payload, default=_canon, indent=2, ensure_ascii=False)
        
        # If raw mode, return plain JSON
        if raw_mode:
            st.text(json_str)
            st.stop()
        
        # Otherwise, auto-download via HTML
        escaped = json_str #html.escape(json_str)#,quote=False)
        filename = f"{country_name}_{start_date}_{end_date}.json"
        
        auto_dl_html = f"""
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8"/>
            <title>Auto-download: {filename}</title>
          </head>
          <body>
            <p>If the download doesn't start automatically, use the link below.</p>
            <a id="dl" href="#" download="{filename}">Download JSON</a>
            <script>
              try {{
                const jsonText = `{escaped}`;
                const blob = new Blob([jsonText], {{ type: 'application/json' }});
                const url = URL.createObjectURL(blob);
                const a = document.getElementById('dl');
                a.href = url;
                a.download = "{filename}";
                a.click();
                setTimeout(() => {{
                  URL.revokeObjectURL(url);
                }}, 1000);
              }} catch (err) {{
                console.error('Auto-download failed', err);
                const p = document.createElement('p');
                p.textContent = 'Automatic download failed — please click the link below.';
                document.body.insertBefore(p, document.getElementById('dl'));
              }}
            </script>
            <noscript>
              <p>JavaScript is disabled in your browser. Click the link to download the JSON.</p>
            </noscript>
          </body>
        </html>
        """
        
        components.html(auto_dl_html, height=200)
        st.stop()
        
    except Exception as e:
        st.json({"error": f"Scraping failed: {str(e)}"})
        st.stop()

# ========== NORMAL UI ==========
st.set_page_config(page_title=APP_TITLE, page_icon="logo.png", layout="wide")
ms_css()

# Sidebar branding
with st.sidebar:
    brand_c1, brand_c2 = st.columns([0.22, 0.78])
    with brand_c1:
        try:
            st.image("logo.png", use_container_width=True)
        except Exception:
            st.write("")
    with brand_c2:
        st.markdown(f'<div class="new-iris-title">{APP_TITLE}</div>', unsafe_allow_html=True)
    st.markdown(f'<div style="border-bottom:2px solid {MS_GRAY}; margin:0.2rem 0 0.8rem 0;"></div>', 
                unsafe_allow_html=True)

# Sidebar controls
st.sidebar.subheader("Controls")
country = st.sidebar.selectbox("Country", COUNTRIES, index=0)

# Date range
today_local = dt.datetime.now(TZ).date()
default_start = today_local - timedelta(days=1)
start_date, end_date = st.sidebar.date_input(
    "Date range (inclusive, Europe/London)",
    value=(default_start, today_local),
    min_value=today_local - timedelta(days=365),
    max_value=today_local
)

# View selection
view = st.sidebar.selectbox("View", options=["User View", "Dev Tools", "API Info"], index=0)

# ---------- API Info View ----------
if view == "API Info":
    st.header("🔌 API Endpoint")
    st.write("Use the URL endpoint to programmatically trigger scraping and download results.")
    
    st.subheader("Usage")
    st.code(f"""
# Basic usage (auto-download JSON in browser)
{st.experimental_get_query_params() or 'https://your-app-url.streamlit.app'}/?action=scrape&country=Serbia

# With custom date range
{st.experimental_get_query_params() or 'https://your-app-url.streamlit.app'}/?action=scrape&country=Serbia&start_date=2024-11-01&end_date=2024-11-10

# Raw JSON output (for curl/programmatic access)
{st.experimental_get_query_params() or 'https://your-app-url.streamlit.app'}/?action=scrape&country=Serbia&raw=1
    """, language="bash")
    
    st.subheader("Parameters")
    st.markdown("""
    - **action** (required): Must be `scrape`
    - **country** (required): Country name (e.g., `Serbia`, `Poland`, `Romania`)
    - **start_date** (optional): Start date in `YYYY-MM-DD` format (default: yesterday)
    - **end_date** (optional): End date in `YYYY-MM-DD` format (default: today)
    - **raw** (optional): Set to `1` or `true` for plain JSON output (useful for curl)
    """)
    
    st.subheader("Example with curl")
    st.code("""
# Download JSON file
curl "https://your-app-url.streamlit.app/?action=scrape&country=Serbia&raw=1" -o results.json

# View JSON directly
curl "https://your-app-url.streamlit.app/?action=scrape&country=Serbia&raw=1"
    """, language="bash")
    
    st.subheader("Available Countries")
    st.write(", ".join(COUNTRIES))
    
    st.subheader("Response Format")
    st.json({
        "country": "Serbia",
        "start_date": "2024-11-11",
        "end_date": "2024-11-12",
        "count": 2,
        "results": [
            {
                "title": "Article title",
                "url": "https://example.com/article",
                "published_utc": "2024-11-12T10:30:00+00:00",
                "source": "example.com"
            }
        ]
    })

# ---------- User View ----------
elif view == "User View":
    st.header("📰 News Scraper")
    
    # Get all available configs
    all_available_configs = set()
    links = load_json_safe(LINKS_FILE)
    for country_val, country_configs in links.items():
        if isinstance(country_configs, list):
            all_available_configs.update(country_configs)
        elif isinstance(country_configs, str):
            all_available_configs.add(country_configs)
    
    if os.path.exists(CONFIGS_DIR):
        for fname in os.listdir(CONFIGS_DIR):
            if fname.endswith('_scrape_config.json'):
                all_available_configs.add(fname)
    
    # Config selection option
    st.subheader("Source Selection")
    col_select1, col_select2 = st.columns(2)
    
    with col_select1:
        scrape_mode = st.radio(
            "Select by:",
            options=["Country (all configs)", "Specific config"],
            index=0
        )
    
    with col_select2:
        if scrape_mode == "Specific config":
            selected_config = st.selectbox(
                "Choose config to test",
                options=["(None)"] + sorted(list(all_available_configs))
            )
        else:
            selected_config = None
    
    # Options
    col1, col2 = st.columns([2, 1])
    with col1:
        mode_choice = st.radio(
            "Data sources", 
            options=["AutoScraper configs only", "GDELT only", "AutoScraper + GDELT"], 
            index=2
        )
    with col2:
        include_gdelt_charts = st.checkbox("Show GDELT charts", value=True)
    
    if st.button("🔎 Scrape now", type="primary"):
        rows_auto, rows_gdelt = [], []
        
        # AutoScraper configs
        if mode_choice in ("AutoScraper configs only", "AutoScraper + GDELT"):
            configs = []
            
            # Load configs based on selection mode
            if scrape_mode == "Specific config" and selected_config and selected_config != "(None)":
                # Load single specific config
                cfg_path = os.path.join(CONFIGS_DIR, selected_config)
                if os.path.exists(cfg_path):
                    cfg = load_json_safe(cfg_path)
                    if cfg:
                        cfg["_filename"] = selected_config
                        configs.append(cfg)
                else:
                    st.error(f"Config file not found: {selected_config}")
            else:
                # Load all configs for selected country
                configs = load_configs_for_country(country)
            
            if not configs:
                st.warning(f"No AutoScraper configs found. "
                          f"Create configs in Dev Tools first.")
            else:
                st.subheader("Scraping AutoScraper configs")
                prog = st.progress(0, text="Starting...")
                
                for i, cfg in enumerate(configs, 1):
                    site_name = cfg.get("site_name", "unknown")
                    prog.progress(i / len(configs), text=f"Scraping {site_name} ({i}/{len(configs)})")
                    
                    with st.status(f"Scraping {site_name}...", expanded=False) as status:
                        items = scrape_with_autoscraper_config(cfg, start_date, end_date)
                        rows_auto.extend(items)
                        
                        # Preview
                        status.write(f"✓ Found {len(items)} items")
                        if items:
                            preview = items[:5]
                            for item in preview:
                                st.markdown(
                                    f'<div class="site-preview">'
                                    f'<strong>{item.get("title", "")[:80]}</strong><br>'
                                    f'<small>{item.get("url", "")}</small>'
                                    f'</div>',
                                    unsafe_allow_html=True
                                )
                        status.update(label=f"✓ {site_name} complete", state="complete")
                
                prog.empty()
                st.success(f"Scraped {len(configs)} config(s). Total items: {len(rows_auto)}")
        
        # GDELT
        if mode_choice in ("GDELT only", "AutoScraper + GDELT"):
            # Only use GDELT if not in specific config mode, or if user wants both
            if scrape_mode != "Specific config" or selected_config == "(None)":
                fips = FIPS_BY_COUNTRY.get(country)
                if not fips:
                    st.error("No FIPS code mapping for selected country.")
                else:
                    st.subheader("Querying GDELT")
                    
                    # Use a container class to hold mutable state
                    class ProgressState:
                        def __init__(self):
                            self.total_seen = 0
                    
                    state = ProgressState()
                    gdelt_box = st.status("Querying GDELT...", expanded=True)
                    prog = st.progress(0, text="Starting GDELT...")

                    def _cb(ev: dict):
                        if ev.get("event") == "batch":
                            state.total_seen = ev.get("total", 0) + ev.get("fetched", 0)
                            gdelt_box.write(
                                f"Batch {ev.get('batch')}: fetched {ev.get('fetched')} | "
                                f"cumulative ~{state.total_seen}"
                            )
                            prog.progress(
                                min(0.99, (ev.get("batch", 1) % 10) / 10), 
                                text=f"GDELT batches: {ev.get('batch')}"
                            )
                        elif ev.get("event") == "warn":
                            gdelt_box.write(f"⚠ {ev.get('message')}")
                        elif ev.get("event") == "error":
                            gdelt_box.write(f"✗ {ev.get('message')}")
                        elif ev.get("event") == "done":
                            gdelt_box.write(f"✓ Total GDELT articles: {ev.get('total')}")
                            prog.progress(1.0, text="GDELT complete")

                    rows_gdelt = gdelt_artlist_rolling(
                        fips, start_date, end_date, 
                        include_json_fields=False, 
                        max_per_call=250, 
                        progress_cb=_cb
                    )
                    gdelt_box.update(label="GDELT complete", state="complete")
                    prog.empty()
            else:
                st.info("GDELT not used when testing specific config. Change to country mode to include GDELT.")
        
        # Combine and deduplicate
        rows_all = dedup_rows(rows_auto + rows_gdelt)
        
        # Downloads
        st.markdown("---")
        st.subheader("📥 Download Results")
        st.caption(f"Total: {len(rows_all)}  •  AutoScraper: {len(rows_auto)}  •  GDELT: {len(rows_gdelt)}")
        
        json_bytes = to_json_bytes(rows_all)
        csv_bytes = to_csv_bytes(rows_all)
        fname_base = f"{country}_{start_date}_{end_date}" if scrape_mode != "Specific config" else f"{selected_config.replace('_scrape_config.json', '')}_{start_date}_{end_date}"
        
        col1, col2 = st.columns(2)
        with col1:
            st.write(rows_all)
            st.download_button(
                "📄 Download JSON", 
                data=json_bytes, 
                file_name=f"{fname_base}.json", 
                mime="application/json"
            )
        with col2:
            st.download_button(
                "📊 Download CSV", 
                data=csv_bytes, 
                file_name=f"{fname_base}.csv", 
                mime="text/csv"
            )
        
        # Articles feed
        st.markdown("---")
        st.subheader("📰 Articles")
        rows_sorted = sorted(
            rows_all, 
            key=lambda r: r.get("published_utc") or dt.datetime.min.replace(tzinfo=dt.timezone.utc), 
            reverse=True
        )
        
        for r in rows_sorted[:100]:  # Show first 100
            pub = r.get("published_utc")
            pub_s = ""
            if isinstance(pub, dt.datetime):
                pub_s = pub.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
            # Add for Source <div><span class="ms-chip">{r.get("via","")}</span></div>
            st.markdown(
                f"""
                <div class="article-card">
                    <div style="display:flex;justify-content:space-between;">
                        <div style="color:{MS_GRAY};font-size:0.85rem;">{pub_s}</div>
                    </div>
                    <div style="margin-top:6px;font-weight:600;">
                        <a href="{r.get("url")}" target="_blank">{r.get("title")}</a>
                    </div>
                    <div style="color:{MS_GRAY};font-size:0.85rem;">{r.get("source","")}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        
        # GDELT charts (only if country mode and GDELT was used)
        if (include_gdelt_charts and mode_choice in ("GDELT only", "AutoScraper + GDELT") 
            and (scrape_mode != "Specific config" or selected_config == "(None)")):
            fips = FIPS_BY_COUNTRY.get(country)
            if fips:
                st.markdown("---")
                st.subheader("📊 GDELT Timelines")
                col1, col2 = st.columns(2)
                with col1:
                    df_vol = gdelt_timeline_csv("TimelineVol", fips, start_date, end_date, smooth=7)
                    if not df_vol.empty:
                        df_vol = df_vol.sort_values("datetime")
                        st.line_chart(df_vol.set_index("datetime")["value"], height=220)
                        st.caption("Volume of matching coverage")
                    else:
                        st.info("No volume data.")
                with col2:
                    df_tone = gdelt_timeline_csv("TimelineTone", fips, start_date, end_date, smooth=7)
                    if not df_tone.empty:
                        df_tone = df_tone.sort_values("datetime")
                        st.line_chart(df_tone.set_index("datetime")["value"], height=220)
                        st.caption("Average tone")
                    else:
                        st.info("No tone data.")

# ---------- Dev Tools ----------
else:  # Dev Tools
    st.header("🛠️ AutoScraper Configuration Manager")
    st.write("Train AutoScraper rules and save them as config files. Configs are stored under `configs/`, "
             "pagination settings in `pagination.json`, and linked to countries via `links.json`.")
    
    # Load links mapping
    links = load_json_safe(LINKS_FILE)
    
    # Get all unique config filenames from links.json and configs directory
    all_existing_configs = set()
    for country_val, country_configs in links.items():
        if isinstance(country_configs, list):
            all_existing_configs.update(country_configs)
        elif isinstance(country_configs, str):
            all_existing_configs.add(country_configs)
    
    # Also scan configs directory for any existing configs
    if os.path.exists(CONFIGS_DIR):
        for fname in os.listdir(CONFIGS_DIR):
            if fname.endswith('.json'):
                all_existing_configs.add(fname)
    
    # Site selection
    st.sidebar.markdown("---")
    st.sidebar.subheader("Site Management")
    site_choice = st.sidebar.selectbox(
        "Choose existing site or create new", 
        ["(New Site)"] + sorted(list(all_existing_configs))
    )
    
    if site_choice == "(New Site)":
        site_name = st.sidebar.text_input("New site name", "")
        url = st.sidebar.text_input("Base URL to scrape (first page)", "")
    else:
        site_name = site_choice
        # Load existing config
        saved_config_path = os.path.join(CONFIGS_DIR, site_choice)
        saved_cfg = {}
        if os.path.exists(saved_config_path):
            saved_cfg = load_json_safe(saved_config_path)
        url = st.sidebar.text_input("Base URL to scrape (first page)", saved_cfg.get("url", ""))
    
    # Country assignment
    st.sidebar.markdown("---")
    st.sidebar.subheader("Country Assignment")
    assigned_countries = []
    if site_name and site_name != "(New Site)":
        assigned_countries = [c for c, cfgs in links.items() 
                             if site_name in (cfgs if isinstance(cfgs, list) else [cfgs])]
    
    assign_country = st.sidebar.multiselect(
        "Assign this config to countries",
        options=COUNTRIES,
        default=assigned_countries,
        help="This config will be available when scraping these countries in User View"
    )
    
    # Training section
    st.subheader("Train AutoScraper")
    st.write("Provide sample elements found on the page (one per line). "
             "AutoScraper will learn rule groups from these.")
    
    samples_text = st.text_area(
        "Sample elements (one per line)", 
        placeholder="https://example.com/article\nArticle title\n01.11.2025",
        height=120
    )
    samples = [s.strip() for s in samples_text.splitlines() if s.strip()]
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        if st.button("🎓 Train Scraper"):
            if not site_name or not url or not samples:
                st.error("Please set a site name, URL and provide at least one sample element.")
            else:
                with st.spinner("Training AutoScraper..."):
                    try:
                        scraper = build_scraper(url, samples)
                        grouped = get_grouped_results(scraper, url)
                        st.session_state.last_grouped = grouped
                        st.session_state.last_scraper_present = True
                        st.session_state.last_autoscraper_obj = scraper
                        st.success("✓ Training complete — review rule groups below.")
                        st.session_state.confirmed_mapping = None
                        st.session_state.confirmed_selected_rule_names = None
                    except Exception as e:
                        st.error(f"Error while training scraper: {e}")
    
    with col2:
        if st.button("🧪 Test scrape"):
            if not st.session_state.last_autoscraper_obj:
                st.error("No trained scraper. Train one first.")
            else:
                with st.spinner("Running test scrape..."):
                    try:
                        grouped = test_scraper(st.session_state.last_autoscraper_obj, url, grouped=True)
                        st.json(grouped)
                    except Exception as e:
                        st.error(f"Test failed: {e}")
    
    # Rule groups display
    if st.session_state.last_grouped:
        st.markdown("---")
        st.subheader("Rule Groups & Field Mapping")
        st.write("Select rule groups to keep and map them to fields.")
        
        # Checkboxes for selection
        selected_local = {}
        for rule_name, values in st.session_state.last_grouped.items():
            label = f"{rule_name} ({len(values)} items) — preview: {values[:3]}"
            keyname = f"sel_{rule_name}"
            checked = st.checkbox(label, value=True, key=keyname)
            selected_local[rule_name] = checked
        st.session_state.selected_rule_names = [k for k, v in selected_local.items() if v]
        
        # Field mapping
        st.markdown("**Field Mapping**")
        inferred = infer_field_mapping(st.session_state.last_grouped)
        
        col_map1, col_map2 = st.columns(2)
        with col_map1:
            st.write("Auto-detected mapping:")
            for r, f in inferred.items():
                st.write(f"• {r} → **{f}**")
        
        with col_map2:
            st.write("Override mapping:")
            manual_mapping = {}
            choices = ["auto", "title", "url", "date", "other"]
            for r in st.session_state.last_grouped.keys():
                sel_key = f"map_{r}"
                pre = st.session_state.get("manual_mapping", {}).get(r, "other")
                try:
                    default_index = choices.index(pre)
                except ValueError:
                    default_index = 4
                manual_mapping[r] = st.selectbox(
                    f"{r}", choices, index=default_index, key=sel_key
                )
            st.session_state.manual_mapping = manual_mapping
        
        # Confirm mapping
        if st.button("✓ Confirm mapping"):
            used_mapping = {}
            for r in inferred.keys():
                choice = st.session_state.manual_mapping.get(r, "other")
                if choice == "auto":
                    used_mapping[r] = inferred.get(r, "other")
                else:
                    used_mapping[r] = choice
            st.session_state.confirmed_mapping = used_mapping
            st.session_state.confirmed_selected_rule_names = st.session_state.get("selected_rule_names", [])
            st.success("✓ Mapping confirmed")
        
        # Preview assembled items
        if st.button("👁️ Preview assembled items"):
            used_mapping_preview = {}
            for r in inferred.keys():
                choice = st.session_state.manual_mapping.get(r, "other")
                if choice == "auto":
                    used_mapping_preview[r] = inferred.get(r, "other")
                else:
                    used_mapping_preview[r] = choice
            assembled = assemble_items_from_grouped(st.session_state.last_grouped, used_mapping_preview)
            st.write(f"Preview of {len(assembled)} items (first 20):")
            st.json(assembled[:20])
    
    # Pagination settings
    st.markdown("---")
    st.subheader("Pagination Settings")
    st.write("Configure pagination to scrape across multiple pages. These settings are saved separately in `pagination.json`.")
    
    # Load existing pagination if editing existing config
    existing_pagination = None
    if site_name and site_name != "(New Site)":
        existing_pagination = get_pagination_for_config(site_name)
    
    collect_pages = st.checkbox("Enable pagination", value=bool(existing_pagination))
    
    if collect_pages:
        page_url_template = st.text_input(
            "Page URL template (use {page})", 
            value=existing_pagination.get("page_url_template", f"{url}?page={{page}}") if existing_pagination else (f"{url}?page={{page}}" if url else ""),
            help="Example: https://example.com/news?page={page}"
        )
        
        col_pag1, col_pag2, col_pag3 = st.columns(3)
        with col_pag1:
            start_page = st.number_input(
                "Start page", 
                value=existing_pagination.get("start_page", 1) if existing_pagination else 1,
                min_value=1, 
                step=1
            )
        with col_pag2:
            max_pages = st.number_input(
                "Max pages", 
                value=existing_pagination.get("max_pages", 10) if existing_pagination else 10,
                min_value=1, 
                step=1
            )
        with col_pag3:
            cutoff_date = st.text_input(
                "Cutoff date (YYYY-MM-DD)", 
                value=existing_pagination.get("cutoff_date", "") if existing_pagination else ""
            )
    else:
        page_url_template = ""
        start_page = 1
        max_pages = 10
        cutoff_date = ""
    
    # Collect items
    if st.button("🔍 Collect items"):
        if not st.session_state.last_autoscraper_obj:
            st.error("No trained scraper. Train first.")
        else:
            scraper = st.session_state.last_autoscraper_obj
            sel_rule_names = (st.session_state.confirmed_selected_rule_names 
                            if st.session_state.confirmed_selected_rule_names is not None 
                            else st.session_state.get("selected_rule_names", []))
            mapping_to_use = (st.session_state.confirmed_mapping 
                            if st.session_state.confirmed_mapping is not None 
                            else None)
            
            if mapping_to_use is None and st.session_state.last_grouped:
                inferred = infer_field_mapping(st.session_state.last_grouped)
                manual = st.session_state.get("manual_mapping", {})
                used_mapping = {}
                for r in inferred.keys():
                    choice = manual.get(r, "other")
                    if choice == "auto":
                        used_mapping[r] = inferred.get(r, "other")
                    else:
                        used_mapping[r] = choice
                mapping_to_use = used_mapping
            
            collected = []
            pages_scraped = 0
            
            if collect_pages and page_url_template:
                with st.spinner("Collecting across pages..."):
                    try:
                        collected, pages_scraped = scrape_pages_collect_items(
                            scraper, page_url_template,
                            start_page=int(start_page),
                            max_pages=int(max_pages),
                            cutoff_date_iso=cutoff_date if cutoff_date else None,
                            mapping=mapping_to_use,
                            selected_rule_names=sel_rule_names
                        )
                    except Exception as e:
                        st.error(f"Pagination collection failed: {e}")
            else:
                if not st.session_state.last_grouped:
                    st.error("No grouped data. Run test scrape first.")
                else:
                    grouped_filtered = st.session_state.last_grouped
                    if sel_rule_names:
                        grouped_filtered = {k: v for k, v in grouped_filtered.items() if k in sel_rule_names}
                    collected = assemble_items_from_grouped(
                        grouped_filtered, mapping_to_use or {}
                    )
                    pages_scraped = 1
            
            # Deduplicate
            seen = set()
            deduped = []
            for it in collected:
                u = it.get("url")
                if u and u in seen:
                    continue
                if u:
                    seen.add(u)
                deduped.append(it)
            
            st.session_state.collected_items = deduped
            st.success(f"✓ Collected {len(deduped)} items across {pages_scraped} pages")
            if deduped:
                st.write("Preview (first 50):")
                st.json(deduped[:50])
    
    # Save config
    st.markdown("---")
    st.subheader("Save Configuration")
    
    default_filename = f"{sanitize_site_name(site_name)}_scrape_config.json" if site_name and site_name != "(New Site)" else ""
    final_filename = st.text_input("Config filename", default_filename)
    
    if st.button("💾 Save config", type="primary"):
        if not site_name or site_name == "(New Site)":
            st.error("Site name is required.")
        elif not st.session_state.last_autoscraper_obj:
            st.error("No trained scraper to save. Train a scraper first.")
        elif not st.session_state.get("collected_items"):
            st.error("No collected items. Use 'Collect items' first.")
        else:
            final_filename = final_filename.strip() or default_filename
            
            # Save the AutoScraper object itself using .save()
            scraper_filename = final_filename.replace("_scrape_config.json", "_scraper.json")
            scraper_path = os.path.join(CONFIGS_DIR, scraper_filename)
            
            try:
                # Save the trained scraper
                st.session_state.last_autoscraper_obj.save(scraper_path)
                
                # Build config JSON that references the scraper file
                final_config = {
                    "site_name": site_name if site_name != "(New Site)" else site_name,
                    "url": url,
                    "scraper_file": scraper_filename,  # Reference to the saved scraper
                    "saved_from_rules": (st.session_state.confirmed_selected_rule_names 
                                        if st.session_state.confirmed_selected_rule_names is not None 
                                        else st.session_state.get("selected_rule_names", [])),
                    "mapping": (st.session_state.confirmed_mapping 
                               if st.session_state.confirmed_mapping is not None 
                               else st.session_state.get("manual_mapping", {})),
                    "filter": None,
                    "items": st.session_state.collected_items  # Keep for reference/preview
                }
                
                final_path = os.path.join(CONFIGS_DIR, final_filename)
                atomic_write_json(final_path, final_config)
                
                # Save pagination settings to pagination.json
                if collect_pages and page_url_template:
                    pagination_settings = {
                        "page_url_template": page_url_template,
                        "start_page": int(start_page),
                        "max_pages": int(max_pages),
                        "cutoff_date": cutoff_date
                    }
                    set_pagination_for_config(final_filename, pagination_settings)
                else:
                    # Remove pagination if disabled
                    set_pagination_for_config(final_filename, None)
                
                # Update links.json for assigned countries
                links = load_json_safe(LINKS_FILE)
                
                # Remove from countries it's no longer assigned to
                for ctry in COUNTRIES:
                    if ctry in links:
                        current = links[ctry]
                        if isinstance(current, list):
                            if final_filename in current and ctry not in assign_country:
                                current.remove(final_filename)
                        elif isinstance(current, str):
                            if current == final_filename and ctry not in assign_country:
                                links[ctry] = []
                
                # Add to newly assigned countries
                for ctry in assign_country:
                    if ctry not in links:
                        links[ctry] = []
                    if isinstance(links[ctry], str):
                        links[ctry] = [links[ctry]]
                    if final_filename not in links[ctry]:
                        links[ctry].append(final_filename)
                
                atomic_write_json(LINKS_FILE, links)
                
                st.success(f"✓ Saved scraper to `configs/{scraper_filename}`, "
                          f"config to `configs/{final_filename}`, "
                          f"pagination to `pagination.json`, and updated country assignments")
                st.write("**Saved config:**")
                st.json(final_config)
                if collect_pages:
                    st.write("**Pagination settings:**")
                    st.json(get_pagination_for_config(final_filename))
                
            except Exception as e:
                st.error(f"Failed to save: {e}")
    
    # Existing configs
    st.markdown("---")
    st.subheader("Manage Existing Configs")
    
    # Get all configs from links.json and configs directory
    all_configs = set()
    for country_val, cfgs in links.items():
        if isinstance(cfgs, list):
            all_configs.update(cfgs)
        elif isinstance(cfgs, str):
            all_configs.add(cfgs)
    
    # Also scan configs directory
    if os.path.exists(CONFIGS_DIR):
        for fname in os.listdir(CONFIGS_DIR):
            if fname.endswith('.json'):
                all_configs.add(fname)
    
    existing_site = st.selectbox("Open config", ["(pick one)"] + sorted(list(all_configs)))
    
    if existing_site and existing_site != "(pick one)":
        cfg_path = os.path.join(CONFIGS_DIR, existing_site)
        if os.path.exists(cfg_path):
            cfg = load_json_safe(cfg_path)
            st.subheader(f"Config: {existing_site}")
            
            # Load pagination for this config
            cfg_pagination = get_pagination_for_config(existing_site)
            
            col_cfg1, col_cfg2 = st.columns(2)
            with col_cfg1:
                st.write("**Site name:**", cfg.get("site_name"))
                st.write("**URL:**", cfg.get("url"))
                st.write("**Rules:**", cfg.get("saved_from_rules"))
                st.write("**Items:**", len(cfg.get("items", [])))
            with col_cfg2:
                st.write("**Mapping:**", cfg.get("mapping"))
                st.write("**Pagination:**", "Enabled" if cfg_pagination else "Disabled")
                if cfg_pagination:
                    st.json(cfg_pagination)
            
            if st.checkbox("Show items"):
                st.json(cfg.get("items", []))
            
            # Edit
            st.markdown("**Edit items**")
            edited_text = st.text_area(
                "Edit items as JSON", 
                value=json.dumps(cfg.get("items", []), indent=2, ensure_ascii=False),
                height=300,
                key=f"edit_{existing_site}"
            )
            if st.button("Save edits", key=f"save_{existing_site}"):
                try:
                    updated_items = json.loads(edited_text)
                    cfg["items"] = updated_items
                    atomic_write_json(cfg_path, cfg)
                    st.success("✓ Saved edits")
                except Exception as e:
                    st.error(f"Failed to save: {e}")
        else:
            st.error("Config file not found.")
    
    # Show links mapping
    st.markdown("---")
    st.subheader("Current Mappings")
    
    col_map_display1, col_map_display2 = st.columns(2)
    with col_map_display1:
        st.write("**Country → Configs (links.json)**")
        st.json(load_json_safe(LINKS_FILE))
    with col_map_display2:
        st.write("**Config → Pagination (pagination.json)**")
        st.json(load_pagination_config())