# Asya-Bot Worklog

---
Task ID: 1
Agent: Main
Task: CRITICAL FIX — NSFW protection for asya-bot channel

Work Log:
- Cloned repository https://github.com/sochiautoparts/asya-bot
- Analyzed all code files to identify root cause of pornographic images in channel
- Found ZERO SafeSearch parameters in ALL image search providers (Bing, Google, SearXNG)
- Found ZERO NSFW/adult content filtering anywhere in the codebase
- Found ZERO image content moderation before posting to channel
- Added SafeSearch=Strict to Bing Images, safe=active to Google Images, safesearch=2 to SearXNG
- Added 50 Russian + 39 English NSFW keywords blocklist in image_fetcher.py
- Added 26 porn/adult domain blacklist in image_fetcher.py
- Added _is_nsfw_query() HARD BLOCK — skips image search entirely for NSFW topics
- Added _is_nsfw_image_url() — blocks downloads from adult domains
- Added _moderate_image_content() AI Vision check in channel.py — checks EVERY image before posting
- AI Vision moderation is fail-safe: if check fails/times out, image is BLOCKED
- Added NSFW keywords to BLOCK_KEYWORDS_RU and BLOCK_KEYWORDS_EN in news.py
- Added NSFW keywords to _validate_post_text() blocked_keywords in channel.py
- Added NSFW domain check to _is_junk_image_url() in channel.py
- Added safesearch parameter to search_searxng() in web_search.py (default=2/strict)
- Cleared image cache (data/image_cache) to remove potentially polluted entries
- All Python files pass syntax check (py_compile)
- All NSFW filter functions tested and passing
- Committed and pushed as d4d7089b
- Dispatched new GitHub Actions run (Run #349, head_sha=d4d7089b)
- Cancelled old Actions run (Run #347, head_sha=870e609c) that was running without NSFW protection

Stage Summary:
- 3 layers of NSFW defense now active: SafeSearch + keyword/domain filtering + AI Vision moderation
- Commit: d4d7089b "CRITICAL FIX: NSFW protection — prevent pornographic images in channel"
- GitHub Actions restarted with fixed code

---
Task ID: 1
Agent: Super Z (main)
Task: Проверить и улучшить весь цикл бота Ася — источники, фото, уникализация, комментарии, посты

Work Log:
- Изучил полную архитектуру бота Ася (news.py, channel.py, content_engine.py, image_fetcher.py, media_handler.py, ai/router.py)
- Добавил 6 русских RSS-источников (ТАСС Авто, РБК Авто, За Рулем, Авто Mail.ru, Колёса.ру, Дром)
- Увеличил лимит альбома с 3 до 10 фото на пост (media_handler.py)
- Расслабил пороги качества изображений (200x150 вместо 300x200, 1KB минимальный размер)
- Включил POOR-качество изображения в альбомы если нет лучших
- Усилил промпт уникализации для русских новостей (обязательный полный пересказ)
- Добавил систему комментирования в группах (comment_on_group_post, auto_comment_in_groups)
- Добавил метод generate_comment() в AI Router с 3-уровневым failover
- Запушил изменения в GitHub и перезапустил Actions (Run #358)

Stage Summary:
- 7 файлов изменено, 287 добавлений, 14 удалений
- GitHub Actions Run #358 запущен успешно
- Ключевые улучшения: больше русских источников, больше фото в постах, сильнее уникализация, новая функция комментариев в группах
---
Task ID: 1
Agent: Main (Super Z)
Task: Проверить источники, исправить мусорные фото, проверить уникализацию и комментарии, перезапустить Actions

Work Log:
- Изучил полную структуру проекта asya-bot (news.py, channel.py, image_fetcher.py, media_handler.py, ai/router.py, config.py)
- Протестировал все 26 RSS-источников: 25 работают, 1 нет (Reddit r/Justrolledintotheshop — 429)
- 6 источников не имеют фото в RSS (требуют скрапинга): Авто Mail.ru, Коммерсант, BBC Sport F1, CarNewsChina, Automotive World, Reddit r/cars
- image_fetcher.py обновлён до v7.0: комплексная фильтрация мусорных фото
  - Минимальные размеры: 300x200 (было 200x150)
  - Минимальный файл: 5KB (было 1KB)
  - Фильтр по соотношению сторон: баннеры (ratio>3:1) и кнопки
  - Фильтр квадратных изображений: аватарки и иконки 300-500px
  - Проверка площади: минимум 40000px
  - JUNK_PATH_KEYWORDS расширен: 60+ паттернов
- news.py: расширен фильтр _JUNK_IMAGE_STEMS и _is_junk_image_url()
- ai/router.py: generate_comment() теперь LOCAL MODEL FIRST
  - Local → Pollinations free → Pollinations key → Cloudflare → Static
  - Добавлен import re
  - Добавлен статический фаллбэк для комментариев
- bot/config.py: удалены неработающие Reddit RSS (r/Justrolledintotheshop — 429, r/cars — 429+нет фото)
- bot/media_handler.py: MIN_WIDTH=300, MIN_HEIGHT=200
- Коммит 8c1bb15 запушен в GitHub
- GitHub Actions Run #364 запущен с новым кодом

Stage Summary:
- 5 файлов изменено, 453 добавления, 53 удаления
- Мусорные фото: жёсткая фильтрация (размеры, соотношение сторон, URL-паттерны, типы контента)
- Комментарии: локальная модель ПЕРВОЙ, облачные API только как фаллбэк
- RSS источники: 24 работающих (2 Reddit удалены)
- Уникализация текста: проверена, работает (7-шаговый процесс)
- GitHub Actions #364 запущен
