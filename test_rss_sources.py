#!/usr/bin/env python3
"""Test all RSS news sources for the Asya bot."""

import httpx
import feedparser
import time
import sys

# All RSS sources from config.py + news.py GLOBAL_RSS_SOURCES
SOURCES = [
    # ── Russian-language RSS sources ──
    {"name": "ТАСС Авто", "url": "https://tass.ru/rss/v2.xml?sections=%D0%90%D0%B2%D1%82%D0%BE"},
    {"name": "Авто Mail.ru", "url": "https://auto.mail.ru/rss/"},
    {"name": "Коммерсант Авто", "url": "https://www.kommersant.ru/RSS/auto.xml"},
    {"name": "5Колесо", "url": "https://5koleso.ru/rss/"},
    # ── International RSS sources ──
    {"name": "Autocar UK", "url": "https://www.autocar.co.uk/rss/News"},
    {"name": "CAR Magazine", "url": "https://www.carmagazine.co.uk/api/rss"},
    {"name": "CnEVPost", "url": "https://cnevpost.com/feed/"},
    {"name": "PaulTan", "url": "https://paultan.org/feed/"},
    {"name": "Autosport", "url": "https://www.autosport.com/rss/f1/news/"},
    {"name": "CarExpert", "url": "https://carexpert.com.au/feed/"},
    {"name": "TheDrive", "url": "https://www.thedrive.com/feed"},
    {"name": "Jalopnik", "url": "https://jalopnik.com/rss"},
    {"name": "AutoExpress", "url": "https://www.autoexpress.co.uk/rss"},
    {"name": "The Autopian", "url": "https://theautopian.com/feed/"},
    {"name": "CarScoops", "url": "https://www.carscoops.com/feed/"},
    {"name": "Motorsport.com", "url": "https://www.motorsport.com/rss/all/news/"},
    {"name": "BBC Sport F1", "url": "https://feeds.bbci.co.uk/sport/formula1/rss.xml"},
    {"name": "CarNewsChina", "url": "https://carnewschina.com/feed/"},
    {"name": "Electrek", "url": "https://electrek.co/feed/"},
    {"name": "Car & Driver", "url": "https://www.caranddriver.com/rss/news.xml"},
    {"name": "Automotive World", "url": "https://www.automotiveworld.com/feed/"},
    {"name": "Reddit r/cars", "url": "https://www.reddit.com/r/cars/.rss"},
    {"name": "Reddit r/MechanicAdvice", "url": "https://www.reddit.com/r/MechanicAdvice/.rss"},
    {"name": "Reddit r/Justrolledintotheshop", "url": "https://www.reddit.com/r/Justrolledintotheshop/.rss"},
    # ── GLOBAL_RSS_SOURCES from news.py ──
    {"name": "Car & Driver Reviews", "url": "https://www.caranddriver.com/rss/reviews.xml"},
    {"name": "Car & Driver Features", "url": "https://www.caranddriver.com/rss/features.xml"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, text/html, */*",
}

def check_images(entry) -> dict:
    """Check various image sources in a feed entry."""
    images = {
        "media_content": False,
        "enclosures": False,
        "img_tags": False,
        "thumbnail": False,
        "any_image": False,
    }
    
    # Check media_content
    if hasattr(entry, "media_content") and entry.media_content:
        images["media_content"] = True
        images["any_image"] = True
    
    # Check enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if hasattr(enc, "type") and enc.type and "image" in enc.type:
                images["enclosures"] = True
                images["any_image"] = True
                break
            if hasattr(enc, "href") and enc.href:
                images["enclosures"] = True
                images["any_image"] = True
                break
    
    # Check img tags in content/summary
    for field in ["summary", "content", "description"]:
        val = None
        if hasattr(entry, field):
            if field == "content" and entry.content:
                val = entry.content[0].get("value", "") if isinstance(entry.content, list) else str(entry.content)
            elif field == "summary":
                val = entry.get("summary", "")
            elif field == "description":
                val = entry.get("description", "")
        if val and "<img" in val:
            images["img_tags"] = True
            images["any_image"] = True
            break
    
    # Check thumbnail
    if hasattr(entry, "thumbnail") and entry.thumbnail:
        images["thumbnail"] = True
        images["any_image"] = True
    
    return images


def test_source(source: dict) -> dict:
    """Test a single RSS source."""
    result = {
        "name": source["name"],
        "url": source["url"],
        "http_status": None,
        "http_ok": False,
        "entries_count": 0,
        "images_found": False,
        "image_details": {},
        "error": None,
        "feed_title": None,
        "response_time_s": None,
    }
    
    start = time.time()
    
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=HEADERS) as client:
            resp = client.get(source["url"])
            result["http_status"] = resp.status_code
            result["http_ok"] = 200 <= resp.status_code < 400
            result["response_time_s"] = round(time.time() - start, 2)
            
            if not result["http_ok"]:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            
            # Parse with feedparser
            feed = feedparser.parse(resp.text)
            
            if feed.bozo and not feed.entries:
                result["error"] = f"Parse error: {feed.bozo_exception}"
                return result
            
            result["feed_title"] = feed.feed.get("title", "(no title)")
            result["entries_count"] = len(feed.entries)
            
            # Check images across all entries
            img_types = {"media_content": 0, "enclosures": 0, "img_tags": 0, "thumbnail": 0}
            for entry in feed.entries:
                imgs = check_images(entry)
                for k, v in imgs.items():
                    if k != "any_image" and v:
                        img_types[k] += 1
            
            result["image_details"] = img_types
            result["images_found"] = any(v > 0 for v in img_types.values())
    
    except httpx.TimeoutException:
        result["error"] = "Timeout (30s)"
        result["response_time_s"] = round(time.time() - start, 2)
    except httpx.ConnectError as e:
        result["error"] = f"Connection error: {e}"
        result["response_time_s"] = round(time.time() - start, 2)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["response_time_s"] = round(time.time() - start, 2)
    
    return result


def main():
    print("=" * 90)
    print("RSS SOURCE TEST REPORT — Asya Bot")
    print("=" * 90)
    
    results = []
    working = 0
    broken = 0
    
    for i, source in enumerate(SOURCES, 1):
        print(f"\n[{i}/{len(SOURCES)}] Testing: {source['name']} ...", end=" ", flush=True)
        result = test_source(source)
        results.append(result)
        
        if result["http_ok"] and result["error"] is None:
            working += 1
            status_icon = "✅"
        else:
            broken += 1
            status_icon = "❌"
        
        print(f"{status_icon} HTTP {result['http_status'] or 'N/A'} | {result['entries_count']} entries | {result['response_time_s']}s")
    
    # ── DETAILED REPORT ──
    print("\n")
    print("=" * 90)
    print("DETAILED RESULTS")
    print("=" * 90)
    
    for r in results:
        status = "OK" if (r["http_ok"] and r["error"] is None) else "FAIL"
        icon = "✅" if status == "OK" else "❌"
        
        print(f"\n{icon} {r['name']}")
        print(f"   URL: {r['url']}")
        print(f"   HTTP Status: {r['http_status'] or 'N/A'}")
        print(f"   Response Time: {r['response_time_s']}s")
        print(f"   Feed Title: {r['feed_title'] or 'N/A'}")
        print(f"   Entries: {r['entries_count']}")
        
        if r["images_found"]:
            img_strs = []
            for k, v in r["image_details"].items():
                if v > 0:
                    img_strs.append(f"{k}={v}/{r['entries_count']}")
            print(f"   Images: YES — {', '.join(img_strs)}")
        else:
            print(f"   Images: NONE")
        
        if r["error"]:
            print(f"   Error: {r['error']}")
    
    # ── SUMMARY TABLE ──
    print("\n")
    print("=" * 90)
    print("SUMMARY TABLE")
    print("=" * 90)
    print(f"{'#':<3} {'Status':<6} {'Name':<30} {'HTTP':<6} {'Entries':<8} {'Images':<8} {'Time':<8} {'Error'}")
    print("-" * 90)
    
    for i, r in enumerate(results, 1):
        status = "OK" if (r["http_ok"] and r["error"] is None) else "FAIL"
        img_status = "YES" if r["images_found"] else "NO"
        err = r["error"] or ""
        if len(err) > 40:
            err = err[:40] + "..."
        print(f"{i:<3} {status:<6} {r['name']:<30} {r['http_status'] or 'N/A':<6} {r['entries_count']:<8} {img_status:<8} {r['response_time_s'] or 'N/A':<8} {err}")
    
    # ── FINAL STATS ──
    print("\n")
    print("=" * 90)
    print("FINAL STATISTICS")
    print("=" * 90)
    print(f"Total sources tested: {len(results)}")
    print(f"Working: {working}")
    print(f"Broken:  {broken}")
    
    entries_total = sum(r["entries_count"] for r in results)
    with_images = sum(1 for r in results if r["images_found"])
    without_images = sum(1 for r in results if not r["images_found"] and r["http_ok"] and r["error"] is None)
    
    print(f"Total entries across all sources: {entries_total}")
    print(f"Sources with images: {with_images}")
    print(f"Sources without images (but working): {without_images}")
    
    # List broken sources
    broken_sources = [r for r in results if not r["http_ok"] or r["error"] is not None]
    if broken_sources:
        print("\n⚠️  BROKEN SOURCES:")
        for r in broken_sources:
            print(f"   - {r['name']}: {r['error'] or f'HTTP {r['http_status']}'}")
    
    # Sources without images
    no_img_sources = [r for r in results if not r["images_found"] and r["http_ok"] and r["error"] is None]
    if no_img_sources:
        print("\n⚠️  SOURCES WITHOUT IMAGES (need scraping fallback):")
        for r in no_img_sources:
            print(f"   - {r['name']} ({r['entries_count']} entries)")
    
    # Sources with 0 entries
    zero_entries = [r for r in results if r["entries_count"] == 0 and r["http_ok"] and r["error"] is None]
    if zero_entries:
        print("\n⚠️  SOURCES WITH 0 ENTRIES (empty feeds):")
        for r in zero_entries:
            print(f"   - {r['name']}: HTTP {r['http_status']}")
    
    return broken


if __name__ == "__main__":
    sys.exit(main())
