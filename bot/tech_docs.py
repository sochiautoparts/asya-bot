"""
Technical Car Documentation Access
Searches online for specs, diagnostic codes, repair procedures, TSBs.
"""

import httpx
import re
import logging
from typing import List, Dict, Optional

from bot.config import config
from bot.web_search import web_search, SearchResult, format_search_results

logger = logging.getLogger("asya.tech_docs")

# ── Technical documentation sources ────────────────────────────────────────────

TECH_DOC_SOURCES = {
    "workshop_manuals": [
        "autozone.com",
        "alldata.com",
        "mitchell1.com",
        "workshopmanuals.org",
        "carmanualshub.com",
        "manualslib.com",
    ],
    "diagnostic_codes": [
        "obd-codes.com",
        "autocodes.com",
        "engine-codes.com",
        "check-engine-light.org",
        "car-info.com",
    ],
    "parts_catalogs": [
        "partsouq.com",
        "catcar.info",
        "7zap.com",
        "ilcats.ru",
        "exist.ru",
        "autopiter.ru",
        "emex.ru",
    ],
    "repair_guides": [
        "repairpal.com",
        "ifixit.com",
        "youtube.com",
        "2carpros.com",
        "justanswer.com",
    ],
    "tsb_recalls": [
        "nhhtsa.gov",
        "nhtsa.gov",
        "carcomplaints.com",
        "arfc.org",
    ],
    "specs": [
        "automobile-catalog.com",
        "cars-data.com",
        "car.info",
        "technicalspecs.net",
    ],
}


async def search_tech_docs(query: str, doc_type: str = "", max_results: int = 5) -> List[SearchResult]:
    """
    Search for technical car documentation online.
    doc_type: workshop_manuals, diagnostic_codes, parts_catalogs, repair_guides, tsb_recalls, specs
    """
    if doc_type and doc_type in TECH_DOC_SOURCES:
        # Search specifically within tech doc sources
        sources = TECH_DOC_SOURCES[doc_type]
        all_results = []

        for source in sources[:3]:  # Try top 3 sources per category
            site_query = f"site:{source} {query}"
            results = await web_search(site_query, max_results=2)
            all_results.extend(results)

        # Also do a general search with technical keywords
        general_query = f"{query} техническая документация ремонт характеристики"
        general_results = await web_search(general_query, max_results=2)
        all_results.extend(general_results)

        return all_results[:max_results]
    else:
        # General tech search across all sources
        tech_query = f"{query} автомобиль технические характеристики ремонт"
        return await web_search(tech_query, max_results=max_results)


async def search_part_by_article(article: str, max_results: int = 5) -> Dict:
    """
    Search for a part by article/OEM number.
    Returns structured info about the part.
    """
    result = {
        "article": article,
        "found": False,
        "name": "",
        "brand": "",
        "applications": [],
        "links": [],
        "prices": [],
    }

    # Search across parts catalogs
    queries = [
        f"запчасть {article} артикул описание",
        f"{article} OEM part number catalog",
    ]

    all_results = []
    for query in queries:
        results = await web_search(query, max_results=3)
        all_results.extend(results)

    # Also try specific parts sites
    site_queries = [
        f"site:partsouq.com {article}",
        f"site:7zap.com {article}",
        f"site:autopiter.ru {article}",
    ]
    for sq in site_queries[:2]:
        results = await web_search(sq, max_results=2)
        all_results.extend(results)

    if all_results:
        result["found"] = True
        result["links"] = [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in all_results[:max_results]
        ]
        # Try to extract part name from snippets
        for r in all_results:
            if r.snippet and len(r.snippet) > 10:
                result["name"] = r.snippet[:100]
                break

    # Add direct catalog links
    result["links"].append({
        "title": f"Partsouq — {article}",
        "url": f"https://partsouq.com/search?q={article}",
        "snippet": "Каталог оригинальных запчастей",
    })
    result["links"].append({
        "title": f"ZZAP — {article}",
        "url": f"https://zzap.ru/search/?q={article}",
        "snippet": "Агрегатор цен на запчасти",
    })

    return result


async def search_diagnostic_code(code: str, car_model: str = "") -> Dict:
    """
    Search for detailed info about an OBD-II diagnostic code,
    optionally specific to a car model.
    """
    from bot.asya import lookup_obd2_code

    result = {
        "code": code,
        "description": lookup_obd2_code(code) or "",
        "car_model": car_model,
        "causes": [],
        "solutions": [],
        "links": [],
    }

    # Search for code details
    queries = [f"ошибка {code} причины устранение"]
    if car_model:
        queries.append(f"{code} {car_model} ошибка ремонт")

    for query in queries:
        search_results = await web_search(query, max_results=3)
        for r in search_results:
            result["links"].append({
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
            })

    return result


async def search_repair_procedure(car_model: str, procedure: str) -> List[SearchResult]:
    """Search for a specific repair procedure for a car model."""
    query = f"{car_model} {procedure} ремонт инструкция как заменить"
    results = await web_search(query, max_results=5)

    # Also try YouTube for video guides
    yt_query = f"site:youtube.com {car_model} {procedure} замена ремонт"
    yt_results = await web_search(yt_query, max_results=2)
    results.extend(yt_results)

    return results[:7]


async def search_car_specs(car_model: str, spec_type: str = "") -> List[SearchResult]:
    """Search for car technical specifications."""
    if spec_type:
        query = f"{car_model} {spec_type} технические характеристики"
    else:
        query = f"{car_model} технические характеристики двигатель размеры"
    return await web_search(query, max_results=5)


async def search_tsb_recall(car_model: str, issue: str = "") -> List[SearchResult]:
    """Search for TSBs (Technical Service Bulletins) and recalls."""
    query = f"{car_model} отзыв сервисный бюллетень TSB"
    if issue:
        query += f" {issue}"
    results = await web_search(query, max_results=5)

    # Also search NHTSA for US models
    nhtsa_query = f"site:nhtsa.gov {car_model} recall TSB"
    nhtsa_results = await web_search(nhtsa_query, max_results=2)
    results.extend(nhtsa_results)

    return results[:7]


def format_tech_context(results: List[SearchResult], query_type: str = "") -> str:
    """Format technical documentation search results for AI context."""
    if not results:
        return "Техническая документация не найдена."

    prefix = f"Результаты поиска ({query_type}):" if query_type else "Найденная техническая информация:"
    return prefix + "\n" + format_search_results(results, max_items=5)


def format_part_info(part_data: Dict) -> str:
    """Format part search results for AI context."""
    lines = [f"Поиск запчасти по артикулу: {part_data['article']}"]

    if part_data.get("name"):
        lines.append(f"Наименование: {part_data['name']}")

    if part_data.get("links"):
        lines.append("Ссылки:")
        for link in part_data["links"][:5]:
            lines.append(f"- {link['title']}: {link['url']}")
            if link.get("snippet"):
                lines.append(f"  {link['snippet'][:150]}")

    if not part_data.get("found"):
        lines.append("Информация по артикулу не найдена, требуется поиск через каталоги.")

    return "\n".join(lines)
