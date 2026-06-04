---
Task ID: 1
Agent: main
Task: Add inline mode, new AI models, enhanced VIN, car profiles, rate limiting, and deploy

Work Log:
- Cloned and read the full asya-bot codebase (12+ Python files)
- Tested Pollinations models via gen.pollinations.ai API
- Created bot/handlers/inline.py — full inline mode support
- Added grok-large, polly, sonar, perplexity-reasoning models to provider
- Changed CHANNEL_POST_INTERVAL_MINUTES from 120 to 10
- Changed CHANNEL_MAX_POSTS_PER_DAY from 12 to 24
- Enhanced VIN decoding with full WMI/VDS/VIS breakdown, region detection, model year, check digit
- Added 50+ new WMI codes (Chinese, Russian brands)
- Added user car profiles: /mycar, /delcar, /mileage commands
- Added car context to AI conversations
- Added rate limiting (10 messages/minute per user)
- Enhanced _search_internet_news with Perplexity AI fallback
- Updated bot commands and help text
- Resolved merge conflicts with remote changes
- Pushed to GitHub and deployed via Actions

Stage Summary:
- Bot running successfully on GitHub Actions (run 26977392627)
- Inline mode verified enabled (@asiaexp_bot supports_inline_queries: True)
- All 9 files changed, 635 insertions
- New models: grok-large, polly, sonar, perplexity-reasoning, step-flash, openai-mini
- Total AI models: 48+ across 6 categories
