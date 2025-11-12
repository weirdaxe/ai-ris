import os
import json
import hashlib
import datetime as dt
import time
from datetime import date
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import streamlit as st
from dateutil import parser as dateparser

# ---------- Constants ----------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

TZ = ZoneInfo("Europe/London")  # inclusive day capping in London time

# Morgan Stanley colors (for UI styling only)
MS_BLUE = "#216CA6"
MS_GRAY = "#7C8A97"
MS_DARK = "#000000"
MS_LIGHT = "#FFFFFF"


def ms_css():
    """Inject Morgan Stanley–style CSS overrides for Streamlit UI."""
    st.markdown(
        f"""
        <style>
            .block-container {{
                padding-top: 0rem !important;
            }}
            header[data-testid="stHeader"] {{
            background: transparent;
            box-shadow: none;}}
            .new-iris-title {{ font-size: 1.4rem; font-weight: 700; color: {MS_DARK}; }}
            .stButton>button {{
                background:{MS_BLUE}; color:{MS_LIGHT}; border:0; border-radius:6px;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def canonicalize_url(url: str) -> str:
    try:
        u = urlparse(url.strip())
        scheme = "https" if u.scheme in ("http", "https") else "https"
        netloc = u.netloc.lower()
        path = u.path or "/"
        q = [(k, v) for k, v in parse_qsl(u.query)
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign",
                                  "utm_term", "utm_content", "gclid", "fbclid"}]
        query = urlencode(q)
        norm = urlunparse((scheme, netloc, path.rstrip("/") or "/", "", "", ""))
        if query:
            norm += "?" + query
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


def within_day_range(dt_utc: dt.datetime, start_d: date, end_d: date) -> bool:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
    local = dt_utc.astimezone(TZ)
    d = local.date()
    return (start_d <= d <= end_d)


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
    return capped
