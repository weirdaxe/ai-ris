import datetime as dt
from datetime import timedelta
import pandas as pd
import streamlit as st

from utils import ms_css, dedup_rows, cap_by_date
from web_scrape import scrape_site


APP_TITLE = "New Iris"

# ---------- UI ----------
st.set_page_config(page_title=APP_TITLE, page_icon="📰", layout="wide")
ms_css()

st.sidebar.subheader("Controls")
test_url = st.sidebar.text_input("Website URL", "https://www.reuters.com")
today = dt.date.today()
start_date, end_date = st.sidebar.date_input(
    "Date range (inclusive, Europe/London)",
    value=(today - timedelta(days=1), today),
    min_value=today - timedelta(days=365),
    max_value=today
)
scrape_btn = st.sidebar.button("Scrape now")


def run_scrape():
    rows = scrape_site(test_url)
    rows = cap_by_date(dedup_rows(rows), start_date, end_date)
    return rows


if scrape_btn:
    rows = run_scrape()
    st.subheader("Articles")
    st.caption(f"Found {len(rows)} articles")

    if rows:
        df = pd.DataFrame([{
            "title": r["title"],
            "url": r["url"],
            "published_utc": r["published_utc"],
            "via": r["via"],
            "source": r["source"],
        } for r in rows])
        st.dataframe(df, use_container_width=True, height=400)
else:
    st.info("Enter a site and press 'Scrape now' to test.")
