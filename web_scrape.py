import requests
from urllib.parse import urlparse
from autoscraper import AutoScraper
# from scrapy import Spider, Browser, HtmlPage
from utils import canonicalize_url, parse_any_datetime


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                  " AppleWebKit/537.36 (KHTML, like Gecko)"
                  " Chrome/122.0 Safari/537.36"
})


def scrape_with_autoscraper(base_url: str, wanted_list=None, max_links=20) -> list[dict]:
    """
    Use AutoScraper to get article links and titles.
    'wanted_list' should be a sample of what you want (e.g., example headlines or urls).
    """
    rows = []
    try:
        scraper = AutoScraper()
        # If no training data provided, guess with generic keywords
        if wanted_list is None:
            wanted_list = ["https://", "Breaking", "News"]

        scraper.build(base_url, wanted_list)
        results = scraper.get_result_similar(base_url, grouped=True)

        # Pick likely candidates
        links = results.get("https://", [])[:max_links]
        titles = results.get("Breaking", []) or results.get("News", [])

        for idx, link in enumerate(links):
            title = titles[idx] if idx < len(titles) else None
            rows.append({
                "title": title or link,
                "url": canonicalize_url(link),
                "published_utc": None,
                "source": urlparse(base_url).netloc,
                "via": "autoscraper"
            })
    except Exception:
        pass
    return rows


def scrape_with_scrapling(base_url: str, max_links=20) -> list[dict]:
    """Use Scrapling (headless browser) to scrape <a> tags for candidate articles."""
    rows = []
    try:
        with Browser() as browser:
            spider = Spider(browser)
            page: HtmlPage = spider.go(base_url)

            anchors = page.css("a")
            seen = set()
            for a in anchors[:max_links * 2]:
                href = a.attrs.get("href")
                text = a.text.strip()
                if href and href.startswith("http") and href not in seen:
                    seen.add(href)
                    rows.append({
                        "title": text or href,
                        "url": canonicalize_url(href),
                        "published_utc": None,
                        "source": urlparse(base_url).netloc,
                        "via": "scrapling"
                    })
                if len(rows) >= max_links:
                    break
    except Exception:
        pass
    return rows


def scrape_site(base: str, max_links: int = 20) -> list[dict]:
    """
    Try AutoScraper first (fast/lightweight).
    If no useful results, fallback to Scrapling.
    """
    rows = scrape_with_autoscraper(base, max_links=max_links)
    if not rows:
        rows = scrape_with_scrapling(base, max_links=max_links)
    return rows
