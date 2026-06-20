#!/usr/bin/env python3
"""Standalone test for Asya bot joke-frequency reduction.

Verifies:
1. get_editorial_aside() returns empty ~92.5% of the time (was 55% empty)
2. get_tone_specific_joke() returns empty for SERIOUS/HYPE/ROUTINE/TECHNICAL tones
3. get_tone_specific_joke() returns a joke only ~40% of the time for FUN tone
4. get_translation_uniquification_hint() no longer has "Шаг 7: Шутка от редакции"
5. channel_prompt_suffix emphasizes INFORMATIVENESS over jokes
6. _trim_excessive_jokes() reduces 2+ joke lines to max 1
7. _is_editorial_joke_line() correctly identifies joke lines
8. Posts with no jokes are left unchanged
9. Posts with 1 joke are left unchanged
10. Posts with 3 jokes get trimmed to 1
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import persona, config
from bot.content_engine import (
    get_editorial_aside,
    get_editorial_team_comment,
    get_tone_specific_joke,
    analyze_news_tone,
    NewsTone,
    get_translation_uniquification_hint,
)
from channel import (
    _trim_excessive_jokes,
    _is_editorial_joke_line,
    _EDITORIAL_JOKE_MARKERS,
    _clean_post_text,
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


print("=" * 70)
print("🧪 Asya Bot — Joke Frequency Reduction Test")
print("=" * 70)
print()

# ─── 1. get_editorial_aside frequency ────────────────────────────────────
print("── 1. get_editorial_aside() — reduced frequency ──")

asides_returned = sum(1 for _ in range(2000) if get_editorial_aside())
pct = asides_returned / 2000 * 100
check(
    f"get_editorial_aside returns joke only ~7.5% of the time (got {pct:.1f}%)",
    pct < 15,  # allow some variance
    f"(got {pct:.1f}%)",
)
check(
    "get_editorial_aside returns empty MOST of the time (>85%)",
    pct < 15,
)
print()

# ─── 2. get_tone_specific_joke — only FUN tone gets jokes ───────────────
print("── 2. get_tone_specific_joke() — only FUN tone ──")

check(
    "SERIOUS tone → no joke",
    get_tone_specific_joke(NewsTone.SERIOUS) == "",
)
check(
    "HYPE tone → no joke (was getting jokes before)",
    get_tone_specific_joke(NewsTone.HYPE) == "",
)
check(
    "ROUTINE tone → no joke (was getting jokes before)",
    get_tone_specific_joke(NewsTone.ROUTINE) == "",
)
check(
    "TECHNICAL tone → no joke (was getting jokes before)",
    get_tone_specific_joke(NewsTone.TECHNICAL) == "",
)

# FUN tone gets jokes ~40% of the time
fun_jokes = sum(1 for _ in range(1000) if get_tone_specific_joke(NewsTone.FUN))
fun_pct = fun_jokes / 1000 * 100
check(
    f"FUN tone → joke ~40% of the time (got {fun_pct:.1f}%)",
    25 < fun_pct < 55,
    f"(got {fun_pct:.1f}%)",
)
print()

# ─── 3. Uniquification hint — no forced joke step ────────────────────────
print("── 3. get_translation_uniquification_hint() — no forced joke ──")

hint_ru = get_translation_uniquification_hint("ru")
check(
    "Russian hint has NO 'Шаг 7: Шутка от редакции'",
    "Шаг 7: Шутка" not in hint_ru and "Шаг 7" not in hint_ru,
)
check(
    "Russian hint has 6-step process (was 7)",
    "6 шагов" in hint_ru,
)
check(
    "Russian hint emphasizes INFORMATIVENESS",
    "ИНФОРМАТИВНЫМ" in hint_ru or "ИНФОРМАТИВНО" in hint_ru,
)
check(
    "Russian hint says jokes are optional (1 пост из 5)",
    "1 пост из 5" in hint_ru,
)

hint_en = get_translation_uniquification_hint("en")
check(
    "English hint has NO 'Шаг 7: Шутка'",
    "Шаг 7: Шутка" not in hint_en and "Шаг 7" not in hint_en,
)
check(
    "English hint has 6-step process",
    "6 шагов" in hint_en,
)
print()

# ─── 4. channel_prompt_suffix — informativeness over jokes ──────────────
print("── 4. channel_prompt_suffix — informative-first ──")

check(
    "channel_prompt_suffix has 'ИНФОРМАТИВНОСТЬ ПРЕВЫШЕ ВСЁ'",
    "ИНФОРМАТИВНОСТЬ ПРЕВЫШЕ ВСЁ" in persona.channel_prompt_suffix,
)
check(
    "channel_prompt_suffix says jokes ~1 пост из 5 (was 1 из 3)",
    "1 пост из 5" in persona.channel_prompt_suffix,
)
check(
    "channel_prompt_suffix says characters ~1 пост из 7 (was not mentioned)",
    "1 пост из 7" in persona.channel_prompt_suffix,
)
check(
    "channel_prompt_suffix no longer says '1 раз из 3'",
    "1 раз из 3" not in persona.channel_prompt_suffix,
)
print()

# ─── 5. _is_editorial_joke_line — joke detection ────────────────────────
print("── 5. _is_editorial_joke_line() — detection ──")

joke_lines = [
    "Мы в редакции уже спорим об этой новости за кофе",
    "Кеша танцует на жёрдочке — новость его развеселила 💃🦜",
    "Лёха отложил гаечный ключ — а он его НИКОГДА не откладывает",
    "Сеньор Помидор мурлычет — а он мурлычет только на хорошие новости",
    "В редакции смеялись до слез 😂",
    "Редакция в шоке: даже кофе не бодрит так, как эта новость!",
]
for line in joke_lines:
    check(
        f"Joke line detected: '{line[:50]}...'",
        _is_editorial_joke_line(line),
    )

news_lines = [
    "BMW представила новый M5 Touring с гибридной установкой на 727 л.с.",
    "Разгон до 100 км/ч занимает 3.5 секунды, максимальная скорость 305 км/ч.",
    "Цена в России составит около 12 миллионов рублей через параллельный импорт.",
    "Это уже третья генерация M5 Touring — предыдущая вышла в 2018 году.",
]
for line in news_lines:
    check(
        f"News line NOT flagged as joke: '{line[:50]}...'",
        not _is_editorial_joke_line(line),
    )
print()

# ─── 6. _trim_excessive_jokes — 0 jokes unchanged ───────────────────────
print("── 6. _trim_excessive_jokes() — 0 jokes unchanged ──")

post_no_jokes = """BMW M5 Touring 2024: гибрид на 727 л.с.

Новая генерация M5 Touring получила гибридную установку: 4.4 V8 + электромотор.
Разгон до 100 км/ч — 3.5 секунды. Максимальная скорость — 305 км/ч.

Цена в России через параллельный импорт — около 12 млн рублей.

Автор @asiaexp_bot"""
trimmed = _trim_excessive_jokes(post_no_jokes)
check(
    "Post with 0 jokes → unchanged",
    trimmed == post_no_jokes,
)
print()

# ─── 7. _trim_excessive_jokes — 1 joke unchanged ────────────────────────
print("── 7. _trim_excessive_jokes() — 1 joke unchanged ──")

post_one_joke = """BMW M5 Touring 2024: гибрид на 727 л.с.

Новая генерация M5 Touring получила гибридную установку: 4.4 V8 + электромотор.
Разгон до 100 км/ч — 3.5 секунды.

Мы в редакции уже спорим об этой новости за кофе.

Автор @asiaexp_bot"""
trimmed = _trim_excessive_jokes(post_one_joke)
check(
    "Post with 1 joke → unchanged",
    trimmed == post_one_joke,
)
print()

# ─── 8. _trim_excessive_jokes — 3 jokes → 1 joke ────────────────────────
print("── 8. _trim_excessive_jokes() — 3 jokes → 1 joke ──")

post_three_jokes = """BMW M5 Touring 2024: гибрид на 727 л.с.

Мы в редакции уже спорим об этой новости за кофе.

Разгон до 100 км/ч — 3.5 секунды.

Кеша танцует на жёрдочке — новость его развеселила 💃🦜

Цена в России — около 12 млн рублей.

В редакции смеялись до слез 😂

Автор @asiaexp_bot"""
trimmed = _trim_excessive_jokes(post_three_jokes)
joke_count_after = sum(1 for line in trimmed.split('\n') if _is_editorial_joke_line(line))
check(
    f"Post with 3 jokes → {joke_count_after} joke line(s) after trim",
    joke_count_after == 1,
    f"(got {joke_count_after})",
)
# Verify news content is preserved
check(
    "News content preserved (727 л.с.)",
    "727 л.с." in trimmed,
)
check(
    "News content preserved (3.5 секунды)",
    "3.5 секунды" in trimmed,
)
check(
    "News content preserved (12 млн рублей)",
    "12 млн рублей" in trimmed,
)
check(
    "Footer preserved",
    "Автор @asiaexp_bot" in trimmed,
)
print()

# ─── 9. _trim_excessive_jokes — 2 jokes → 1 joke ────────────────────────
print("── 9. _trim_excessive_jokes() — 2 jokes → 1 joke ──")

post_two_jokes = """Tesla Model S Plaid: 1020 л.с. и разгон 2.1 сек

Лёха отложил гаечный ключ — а он его НИКОГДА не откладывает 🔧

Электромоторы суммарно выдают 1020 л.с., разгон до 100 — 2.1 секунды.

Сеньор Помидор мурлычет — а он мурлычет только на хорошие новости

Автор @asiaexp_bot"""
trimmed = _trim_excessive_jokes(post_two_jokes)
joke_count_after = sum(1 for line in trimmed.split('\n') if _is_editorial_joke_line(line))
check(
    f"Post with 2 jokes → {joke_count_after} joke line(s) after trim",
    joke_count_after == 1,
    f"(got {joke_count_after})",
)
check(
    "First joke is preserved (Лёха)",
    "Лёха отложил" in trimmed,
)
check(
    "Second joke is removed (Помидор line gone)",
    "Сеньор Помидор мурлычет" not in trimmed,
)
print()

# ─── 10. Empty/None handling ────────────────────────────────────────────
print("── 10. Edge cases — empty/None handling ──")

check(
    "_trim_excessive_jokes('') → ''",
    _trim_excessive_jokes("") == "",
)
check(
    "_trim_excessive_jokes(None) → None",
    _trim_excessive_jokes(None) is None,
)
check(
    "_is_editorial_joke_line('') → False",
    _is_editorial_joke_line("") is False,
)
check(
    "_is_editorial_joke_line(None) → False",
    _is_editorial_joke_line(None) is False,
)
print()

# ─── 11. Editorial team comment is now RARELY used ──────────────────────
print("── 11. Editorial team comments — rarely injected ──")

# get_editorial_aside no longer falls back to get_editorial_team_comment
# (it only returns from persona.editorial_asides, and only 7.5% of the time)
import inspect
src = inspect.getsource(get_editorial_aside)
check(
    "get_editorial_aside no longer calls get_editorial_team_comment",
    "get_editorial_team_comment" not in src,
)
print()

# ─── 12. Telegram limits respected ──────────────────────────────────────
print("── 12. Telegram limits in config ──")

check(
    f"TELEGRAM_CAPTION_LIMIT = 1024 (got {config.TELEGRAM_CAPTION_LIMIT})",
    config.TELEGRAM_CAPTION_LIMIT == 1024,
)
check(
    f"TELEGRAM_TEXT_LIMIT = 4096 (got {config.TELEGRAM_TEXT_LIMIT})",
    config.TELEGRAM_TEXT_LIMIT == 4096,
)
print()

# ─── SUMMARY ────────────────────────────────────────────────────────────
print("=" * 70)
print(f"📊 RESULTS: {passed} ✅  |  {failed} ❌")
print("=" * 70)
if failed == 0:
    print("🎉 ALL CHECKS PASSED — Asya bot now produces INFORMATIVE posts!")
    print("   + Editorial asides: ~7.5% of posts (was ~55%)")
    print("   + Tone jokes: only FUN tone, ~40% chance (was all non-serious)")
    print("   + 7-step → 6-step process (no forced joke step)")
    print("   + _trim_excessive_jokes: max 1 joke line per post")
    print("   + Prompts emphasize INFORMATIVENESS over jokes")
    print("   + News content always preserved when trimming jokes")
else:
    print("⚠️  Some checks failed — review above.")
sys.exit(0 if failed == 0 else 1)
