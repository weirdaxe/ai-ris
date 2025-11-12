# scraper_utils.py
from autoscraper import AutoScraper
from typing import List, Dict, Any, Tuple, Optional
import re
from datetime import datetime

COMMON_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
]


def build_scraper(url: str, sample_list: List[str]) -> AutoScraper:
    """Train an AutoScraper on the given URL with sample elements"""
    scraper = AutoScraper()
    scraper.build(url, sample_list)
    return scraper


def get_grouped_results(scraper: AutoScraper, url: str) -> Dict[str, list]:
    """
    Return grouped results (rule_name -> list of values)
    Uses get_result_similar(..., grouped=True)
    """
    return scraper.get_result_similar(url, grouped=True)


def test_scraper(scraper: AutoScraper, url: str, grouped: bool = True) -> Dict:
    if grouped:
        return get_grouped_results(scraper, url)
    else:
        return {"results": scraper.get_result_similar(url)}


# --------- Helpers for mapping groups to title/url/date ----------

URL_RE = re.compile(r"^https?://", re.I)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def looks_like_url(s: str) -> bool:
    return bool(URL_RE.search(s.strip()))


def parse_date_string(s: str) -> Optional[str]:
    """
    Try to parse `s` into ISO date 'YYYY-MM-DD'. Returns None if fails.
    Uses several common formats and a fallback ISO substring search.
    """
    if not s or not isinstance(s, str):
        return None
    s_clean = s.strip()
    # try direct ISO substring first
    m = ISO_DATE_RE.search(s_clean)
    if m:
        try:
            dt = datetime.strptime(m.group(0), "%Y-%m-%d")
            return dt.date().isoformat()
        except Exception:
            pass
    # try common formats
    for fmt in COMMON_DATE_FORMATS:
        try:
            dt = datetime.strptime(s_clean, fmt)
            return dt.date().isoformat()
        except Exception:
            continue
    # try to extract something like 01.02.2021
    m2 = re.search(r"(\d{1,2}[./]\d{1,2}[./]\d{2,4})", s_clean)
    if m2:
        candidate = m2.group(1)
        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%y"):
            try:
                dt = datetime.strptime(candidate, fmt)
                return dt.date().isoformat()
            except Exception:
                continue
    # last resort: textual month like "Jan 2, 2021"
    m3 = re.search(r"([A-Za-z]{3,9} \d{1,2}, \d{4})", s_clean)
    if m3:
        try:
            dt = datetime.strptime(m3.group(1), "%B %d, %Y")
            return dt.date().isoformat()
        except Exception:
            try:
                dt = datetime.strptime(m3.group(1), "%b %d, %Y")
                return dt.date().isoformat()
            except Exception:
                pass
    return None


def infer_field_mapping(grouped: Dict[str, List[str]]) -> Dict[str, str]:
    """
    Heuristically decide which group looks like 'url', 'date', 'title'.
    Returns mapping rule_name -> field ('url'|'date'|'title'|'other').
    """
    metrics = {}
    for rule, vals in grouped.items():
        vals_nonempty = [v for v in vals if isinstance(v, str) and v.strip()]
        if not vals_nonempty:
            metrics[rule] = {"url_frac": 0.0, "date_frac": 0.0, "avg_len": 0.0}
            continue
        url_frac = sum(1 for v in vals_nonempty if looks_like_url(v)) / len(vals_nonempty)
        date_frac = sum(1 for v in vals_nonempty if parse_date_string(v) is not None) / len(vals_nonempty)
        avg_len = sum(len(v) for v in vals_nonempty) / len(vals_nonempty)
        metrics[rule] = {"url_frac": url_frac, "date_frac": date_frac, "avg_len": avg_len}

    # pick best url rule
    mapping = {}
    url_candidate = max(metrics.keys(), key=lambda r: (metrics[r]["url_frac"], metrics[r]["avg_len"]))
    if metrics[url_candidate]["url_frac"] > 0.2:
        mapping[url_candidate] = "url"

    # pick best date rule (exclude url_candidate)
    date_candidates = [r for r in metrics.keys() if r not in mapping]
    if date_candidates:
        date_candidate = max(date_candidates, key=lambda r: (metrics[r]["date_frac"], -metrics[r]["avg_len"]))
        if metrics[date_candidate]["date_frac"] > 0.15:
            mapping[date_candidate] = "date"

    # remaining rules -> title by avg_len
    for r in metrics.keys():
        if r in mapping:
            continue
        mapping[r] = "title" if metrics[r]["avg_len"] > 10 else "other"

    return mapping


def assemble_items_from_grouped(grouped: Dict[str, List[str]], mapping: Dict[str, str]) -> List[Dict[str, Optional[str]]]:
    """
    Given grouped results and a mapping rule->field, build list of item dicts
    [{'title':..., 'url':..., 'date': 'YYYY-MM-DD' or None}, ...]
    Works by zipping groups: finds max length among url/title/date groups and aligns by index.
    """
    # pick lists for each field
    field_lists = {"url": [], "title": [], "date": []}
    for rule, field in mapping.items():
        if field in field_lists:
            field_lists[field] = grouped.get(rule, [])

    # Determine length to iterate: prefer url length, otherwise max length
    lengths = [len(field_lists[f]) for f in field_lists]
    target_len = max(lengths) if lengths else 0

    items = []
    for i in range(target_len):
        raw_url = field_lists["url"][i] if i < len(field_lists["url"]) else None
        raw_title = field_lists["title"][i] if i < len(field_lists["title"]) else None
        raw_date = field_lists["date"][i] if i < len(field_lists["date"]) else None

        parsed_date = parse_date_string(raw_date) if raw_date else None

        # if url is missing but title contains an http-like string, promote it
        if not raw_url and isinstance(raw_title, str) and looks_like_url(raw_title):
            raw_url = raw_title

        item = {"title": raw_title, "url": raw_url, "date": parsed_date}
        items.append(item)
    return items


# -------- Pagination helper --------

def scrape_pages_collect_items(
    scraper: AutoScraper,
    page_url_template: str,
    start_page: int = 1,
    max_pages: int = 10,
    cutoff_date_iso: Optional[str] = None,
    mapping: Optional[Dict[str, str]] = None,
    selected_rule_names: Optional[List[str]] = None,
):
    """
    Iterate pages using page_url_template with '{page}' replaced.
    Uses provided mapping if given; otherwise infers from first page.
    Can limit pages using cutoff_date_iso if dates are available.
    If no dates are found, iterates through all max_pages.
    selected_rule_names: if given, only uses these groups when assembling items.
    Returns (collected_items, pages_scraped)
    """
    collected = []
    seen_urls = set()
    pages_scraped = 0
    cutoff_dt = None
    has_any_dates = False  # Track if we've seen any dates at all
    
    if cutoff_date_iso:
        try:
            cutoff_dt = datetime.strptime(cutoff_date_iso, "%Y-%m-%d").date()
        except Exception:
            cutoff_dt = None

    for p in range(start_page, start_page + max_pages):
        page_url = page_url_template.format(page=p)
        try:
            grouped = scraper.get_result_similar(page_url, grouped=True)
        except Exception:
            # If scraping fails, stop pagination
            break

        # Filter grouped to selected_rule_names if provided
        if selected_rule_names:
            grouped = {k: v for k, v in grouped.items() if k in selected_rule_names}

        # Use mapping if provided, otherwise infer
        active_mapping = mapping or infer_field_mapping(grouped)

        items = assemble_items_from_grouped(grouped, active_mapping)

        page_oldest_date = None
        for it in items:
            u = it.get("url")
            if u and u in seen_urls:
                continue
            if u:
                seen_urls.add(u)
            collected.append(it)

            d = it.get("date")
            if d:
                has_any_dates = True  # We found at least one date
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d").date()
                    if page_oldest_date is None or dt < page_oldest_date:
                        page_oldest_date = dt
                except Exception:
                    pass

        pages_scraped += 1

        # Only stop early if:
        # 1. We have a cutoff date configured
        # 2. We found dates on this page
        # 3. The oldest date is before cutoff
        if cutoff_dt and page_oldest_date and page_oldest_date < cutoff_dt:
            break
        
        # If no items found on page, stop
        if not items:
            break

    return collected, pages_scraped

# # scraper_utils.py
# from autoscraper import AutoScraper
# from typing import List, Dict, Any, Tuple, Optional
# import re
# from datetime import datetime

# COMMON_DATE_FORMATS = [
#     "%Y-%m-%d",
#     "%d.%m.%Y",
#     "%d.%m.%y",
#     "%d/%m/%Y",
#     "%m/%d/%Y",
#     "%Y/%m/%d",
#     "%b %d, %Y",
#     "%B %d, %Y",
#     "%d %b %Y",
#     "%d %B %Y",
# ]


# def build_scraper(url: str, sample_list: List[str]) -> AutoScraper:
#     """Train an AutoScraper on the given URL with sample elements"""
#     scraper = AutoScraper()
#     scraper.build(url, sample_list)
#     return scraper


# def get_grouped_results(scraper: AutoScraper, url: str) -> Dict[str, list]:
#     """
#     Return grouped results (rule_name -> list of values)
#     Uses get_result_similar(..., grouped=True)
#     """
#     return scraper.get_result_similar(url, grouped=True)


# def test_scraper(scraper: AutoScraper, url: str, grouped: bool = True) -> Dict:
#     if grouped:
#         return get_grouped_results(scraper, url)
#     else:
#         return {"results": scraper.get_result_similar(url)}


# # --------- Helpers for mapping groups to title/url/date ----------

# URL_RE = re.compile(r"^https?://", re.I)
# ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# def looks_like_url(s: str) -> bool:
#     return bool(URL_RE.search(s.strip()))


# def parse_date_string(s: str) -> Optional[str]:
#     """
#     Try to parse `s` into ISO date 'YYYY-MM-DD'. Returns None if fails.
#     Uses several common formats and a fallback ISO substring search.
#     """
#     if not s or not isinstance(s, str):
#         return None
#     s_clean = s.strip()
#     # try direct ISO substring first
#     m = ISO_DATE_RE.search(s_clean)
#     if m:
#         try:
#             dt = datetime.strptime(m.group(0), "%Y-%m-%d")
#             return dt.date().isoformat()
#         except Exception:
#             pass
#     # try common formats
#     for fmt in COMMON_DATE_FORMATS:
#         try:
#             dt = datetime.strptime(s_clean, fmt)
#             return dt.date().isoformat()
#         except Exception:
#             continue
#     # try to extract something like 01.02.2021
#     m2 = re.search(r"(\d{1,2}[./]\d{1,2}[./]\d{2,4})", s_clean)
#     if m2:
#         candidate = m2.group(1)
#         for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%y"):
#             try:
#                 dt = datetime.strptime(candidate, fmt)
#                 return dt.date().isoformat()
#             except Exception:
#                 continue
#     # last resort: textual month like "Jan 2, 2021"
#     m3 = re.search(r"([A-Za-z]{3,9} \d{1,2}, \d{4})", s_clean)
#     if m3:
#         try:
#             dt = datetime.strptime(m3.group(1), "%B %d, %Y")
#             return dt.date().isoformat()
#         except Exception:
#             try:
#                 dt = datetime.strptime(m3.group(1), "%b %d, %Y")
#                 return dt.date().isoformat()
#             except Exception:
#                 pass
#     return None


# def infer_field_mapping(grouped: Dict[str, List[str]]) -> Dict[str, str]:
#     """
#     Heuristically decide which group looks like 'url', 'date', 'title'.
#     Returns mapping rule_name -> field ('url'|'date'|'title'|'other').
#     """
#     metrics = {}
#     for rule, vals in grouped.items():
#         vals_nonempty = [v for v in vals if isinstance(v, str) and v.strip()]
#         if not vals_nonempty:
#             metrics[rule] = {"url_frac": 0.0, "date_frac": 0.0, "avg_len": 0.0}
#             continue
#         url_frac = sum(1 for v in vals_nonempty if looks_like_url(v)) / len(vals_nonempty)
#         date_frac = sum(1 for v in vals_nonempty if parse_date_string(v) is not None) / len(vals_nonempty)
#         avg_len = sum(len(v) for v in vals_nonempty) / len(vals_nonempty)
#         metrics[rule] = {"url_frac": url_frac, "date_frac": date_frac, "avg_len": avg_len}

#     # pick best url rule
#     mapping = {}
#     url_candidate = max(metrics.keys(), key=lambda r: (metrics[r]["url_frac"], metrics[r]["avg_len"]))
#     if metrics[url_candidate]["url_frac"] > 0.2:
#         mapping[url_candidate] = "url"

#     # pick best date rule (exclude url_candidate)
#     date_candidates = [r for r in metrics.keys() if r not in mapping]
#     if date_candidates:
#         date_candidate = max(date_candidates, key=lambda r: (metrics[r]["date_frac"], -metrics[r]["avg_len"]))
#         if metrics[date_candidate]["date_frac"] > 0.15:
#             mapping[date_candidate] = "date"

#     # remaining rules -> title by avg_len
#     for r in metrics.keys():
#         if r in mapping:
#             continue
#         mapping[r] = "title" if metrics[r]["avg_len"] > 10 else "other"

#     return mapping


# def assemble_items_from_grouped(grouped: Dict[str, List[str]], mapping: Dict[str, str]) -> List[Dict[str, Optional[str]]]:
#     """
#     Given grouped results and a mapping rule->field, build list of item dicts
#     [{'title':..., 'url':..., 'date': 'YYYY-MM-DD' or None}, ...]
#     Works by zipping groups: finds max length among url/title/date groups and aligns by index.
#     """
#     # pick lists for each field
#     field_lists = {"url": [], "title": [], "date": []}
#     for rule, field in mapping.items():
#         if field in field_lists:
#             field_lists[field] = grouped.get(rule, [])

#     # Determine length to iterate: prefer url length, otherwise max length
#     lengths = [len(field_lists[f]) for f in field_lists]
#     target_len = max(lengths)

#     items = []
#     for i in range(target_len):
#         raw_url = field_lists["url"][i] if i < len(field_lists["url"]) else None
#         raw_title = field_lists["title"][i] if i < len(field_lists["title"]) else None
#         raw_date = field_lists["date"][i] if i < len(field_lists["date"]) else None

#         parsed_date = parse_date_string(raw_date) if raw_date else None

#         # if url is missing but title contains an http-like string, promote it
#         if not raw_url and isinstance(raw_title, str) and looks_like_url(raw_title):
#             raw_url = raw_title

#         item = {"title": raw_title, "url": raw_url, "date": parsed_date}
#         items.append(item)
#     return items


# # -------- Pagination helper --------

# def scrape_pages_collect_items(
#     scraper: AutoScraper,
#     page_url_template: str,
#     start_page: int = 1,
#     max_pages: int = 10,
#     cutoff_date_iso: Optional[str] = None,
#     mapping: Optional[Dict[str, str]] = None,
#     selected_rule_names: Optional[List[str]] = None,  # <- add this
# ):
#     """
#     Iterate pages using page_url_template with '{page}' replaced.
#     Uses provided mapping if given; otherwise infers from first page.
#     Can limit pages using cutoff_date_iso.
#     selected_rule_names: if given, only uses these groups when assembling items.
#     Returns (collected_items, pages_scraped)
#     """
#     collected = []
#     seen_urls = set()
#     pages_scraped = 0
#     cutoff_dt = None
#     if cutoff_date_iso:
#         try:
#             cutoff_dt = datetime.strptime(cutoff_date_iso, "%Y-%m-%d").date()
#         except Exception:
#             cutoff_dt = None

#     for p in range(start_page, start_page + max_pages):
#         page_url = page_url_template.format(page=p)
#         try:
#             grouped = scraper.get_result_similar(page_url, grouped=True)
#         except Exception:
#             break

#         # Filter grouped to selected_rule_names if provided
#         if selected_rule_names:
#             grouped = {k: v for k, v in grouped.items() if k in selected_rule_names}

#         # Use mapping if provided, otherwise infer
#         active_mapping = mapping or infer_field_mapping(grouped)

#         items = assemble_items_from_grouped(grouped, active_mapping)

#         page_oldest_date = None
#         for it in items:
#             u = it.get("url")
#             if u and u in seen_urls:
#                 continue
#             if u:
#                 seen_urls.add(u)
#             collected.append(it)

#             d = it.get("date")
#             if d:
#                 try:
#                     dt = datetime.strptime(d, "%Y-%m-%d").date()
#                     if page_oldest_date is None or dt < page_oldest_date:
#                         page_oldest_date = dt
#                 except Exception:
#                     pass

#         pages_scraped += 1

#         # stop if cutoff date is reached
#         if cutoff_dt and page_oldest_date and page_oldest_date < cutoff_dt:
#             break

#     return collected, pages_scraped