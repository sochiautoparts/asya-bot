---
Task ID: 1
Agent: main
Task: Fix photos in posts — comprehensive image pipeline fixes for Asya bot

Work Log:
- Diagnosed root cause: most posts without photos because news come from web_search/Google News with image_urls=[]
- RSS sources that DO have images (5Колесо, Autocar, CAR Magazine, TheDrive, etc.) work correctly with _extract_entry_images()
- image_fetcher.py was too strict: 400x300 minimum dimensions blocked BBC thumbnails, area<100000 too aggressive
- TINY_SIZE_PATTERNS was matching real image sizes like /640x360/ and -1024x521
- JUNK_PATH_KEYWORDS contained "thumb_" and "preview_" matching legit WordPress URLs
- BBC thumbnails at 240px were being rejected; after upgrade to /640/ they become 640x360
- CarExpert images are 7008x4672 — needed auto-resize
- Automotive World RSS had no images at all — removed
- Added second-pass image enrichment in content_engine for items with 0 images
- channel.py now uses pre-fetched images from content_engine
- Database busy_timeout increased from 5000 to 10000ms
- Pushed fixes and restarted GitHub Actions

Stage Summary:
- Modified files: news.py, image_fetcher.py, content_engine.py, channel.py, database.py, config.py
- GitHub Actions workflow run ID: 27469238119 (in_progress)
- Key fix: images now extracted and downloaded from RSS feeds that have them
- Key fix: items without images get second-pass enrichment via ImageFetcher pipeline
- Key fix: BBC thumbnails upgraded to 640px, large images auto-resized
