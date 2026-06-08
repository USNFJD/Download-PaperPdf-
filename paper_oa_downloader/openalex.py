from __future__ import annotations

import math
import re
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from . import runtime_log


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_PUBLISHERS_URL = "https://api.openalex.org/publishers"
OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        for offset in offsets:
            positions.append((offset, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def clean_abstract(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"</?(?:jats:)?(?:p|sec|title|italic|bold|sub|sup|xref|break|inline-formula|disp-formula)[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def first_pdf_url(work: dict[str, Any]) -> str:
    locations = []
    for key in ("primary_location", "best_oa_location"):
        value = work.get(key)
        if isinstance(value, dict):
            locations.append(value)
    locations.extend(work.get("locations") or [])

    for location in locations:
        pdf_url = location.get("pdf_url") if isinstance(location, dict) else None
        if pdf_url:
            return pdf_url

    oa_url = (work.get("open_access") or {}).get("oa_url") or ""
    if oa_url.lower().split("?")[0].endswith(".pdf"):
        return oa_url
    return ""


def work_to_candidate(work: dict[str, Any], query: str) -> dict[str, Any] | None:
    open_access = work.get("open_access") or {}
    if work.get("is_retracted") is True:
        return None

    pdf_url = first_pdf_url(work)
    if not str(pdf_url).lower().startswith(("http://", "https://")):
        pdf_url = ""

    authorships = work.get("authorships") or []
    authors = [
        ((auth.get("author") or {}).get("display_name") or "").strip()
        for auth in authorships
        if (auth.get("author") or {}).get("display_name")
    ]
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    source_url = open_access.get("oa_url") or primary.get("landing_page_url") or work.get("id") or ""

    return {
        "id": work.get("id") or work.get("doi") or pdf_url or source_url or work.get("title"),
        "doi": work.get("doi"),
        "title": work.get("title") or "Untitled",
        "authors": authors,
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "publication_date": work.get("publication_date") or "",
        "journal": source.get("display_name") or "",
        "publisher": source.get("host_organization_name") or "",
        "issn_l": source.get("issn_l"),
        "issns": source.get("issn") or [],
        "pdf_url": pdf_url,
        "source_url": source_url,
        "is_oa": bool(open_access.get("is_oa")),
        "oa_status": open_access.get("oa_status") or "",
        "is_retracted": bool(work.get("is_retracted")),
        "relevance_score": float(work.get("relevance_score") or 0),
        "cited_by_count": int(work.get("cited_by_count") or 0),
        "query": query,
    }


class OpenAlexClient:
    def __init__(self, api_key: str = "", mailto: str = ""):
        self.api_key = api_key.strip()
        self.mailto = mailto.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "MarLous-Paper-Downloader/1.0 (mailto optional)",
                "Accept": "application/json",
            }
        )
        self.abstract_cache: dict[str, str] = {}

    def normalize_doi(self, doi: str) -> str:
        clean_doi = str(doi or "").strip()
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi, flags=re.I)

    def search(
        self,
        query: str,
        per_page: int = 50,
        sort: str = "relevance_score:desc",
        filters: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        runtime_log.write(
            f"OpenAlex search started. query={query}; per_page={per_page}; sort={sort}; filters={filters or []}",
            "openalex",
        )
        filter_parts = ["type:article", "is_retracted:false", *(filters or [])]
        params: dict[str, Any] = {
            "search": query,
            "filter": ",".join(filter_parts),
            "per-page": max(1, min(per_page, 200)),
            "sort": sort,
        }
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key

        response = self.session.get(OPENALEX_WORKS_URL, params=params, timeout=12)
        if response.status_code == 403:
            raise RuntimeError("OpenAlex returned 403. Please set an OpenAlex API key and try again.")
        response.raise_for_status()
        data = response.json()
        candidates = [
            candidate
            for work in data.get("results", [])
            if (candidate := work_to_candidate(work, query))
        ]
        runtime_log.write(
            f"OpenAlex search finished. query={query}; raw={len(data.get('results', []))}; kept={len(candidates)}",
            "openalex",
        )
        return candidates

    def search_title(
        self,
        title: str,
        per_page: int = 50,
        sort: str = "relevance_score:desc",
        filters: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        runtime_log.write(
            f"OpenAlex title search started. title={title}; per_page={per_page}; sort={sort}; filters={filters or []}",
            "openalex",
        )
        clean_title = " ".join(str(title or "").replace(",", " ").split())
        filter_parts = [
            "type:article",
            "is_retracted:false",
            f"title.search:{clean_title}",
            *(filters or []),
        ]
        params: dict[str, Any] = {
            "filter": ",".join(filter_parts),
            "per-page": max(1, min(per_page, 200)),
            "sort": sort,
        }
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key

        response = self.session.get(OPENALEX_WORKS_URL, params=params, timeout=12)
        if response.status_code == 403:
            raise RuntimeError("OpenAlex returned 403. Please set an OpenAlex API key and try again.")
        response.raise_for_status()
        data = response.json()
        candidates = [
            candidate
            for work in data.get("results", [])
            if (candidate := work_to_candidate(work, title))
        ]
        runtime_log.write(
            f"OpenAlex title search finished. title={title}; raw={len(data.get('results', []))}; kept={len(candidates)}",
            "openalex",
        )
        return candidates

    def crossref_abstract(self, doi: str) -> str:
        clean_doi = self.normalize_doi(doi)
        if not clean_doi:
            return ""
        key = f"crossref:{clean_doi.casefold()}"
        if key in self.abstract_cache:
            return self.abstract_cache[key]
        params: dict[str, Any] = {}
        if self.mailto:
            params["mailto"] = self.mailto
        try:
            response = self.session.get(
                f"{CROSSREF_WORKS_URL}/{quote(clean_doi, safe='')}",
                params=params,
                timeout=8,
            )
            if response.status_code == 404:
                self.abstract_cache[key] = ""
                return ""
            response.raise_for_status()
            message = (response.json() or {}).get("message") or {}
            abstract = clean_abstract(message.get("abstract"))
        except Exception as exc:
            runtime_log.write(f"Crossref abstract lookup ignored. doi={clean_doi}; error={exc}", "openalex")
            abstract = ""
        self.abstract_cache[key] = abstract
        return abstract

    def semantic_scholar_abstract(self, doi: str) -> str:
        clean_doi = self.normalize_doi(doi)
        if not clean_doi:
            return ""
        key = f"semantic:{clean_doi.casefold()}"
        if key in self.abstract_cache:
            return self.abstract_cache[key]
        try:
            response = self.session.get(
                f"{SEMANTIC_SCHOLAR_PAPER_URL}/DOI:{quote(clean_doi, safe='')}",
                params={"fields": "abstract"},
                timeout=8,
            )
            if response.status_code in {404, 429}:
                self.abstract_cache[key] = ""
                return ""
            response.raise_for_status()
            abstract = clean_abstract((response.json() or {}).get("abstract"))
        except Exception as exc:
            runtime_log.write(f"Semantic Scholar abstract lookup ignored. doi={clean_doi}; error={exc}", "openalex")
            abstract = ""
        self.abstract_cache[key] = abstract
        return abstract

    def fallback_abstract(self, doi: str) -> str:
        return self.crossref_abstract(doi) or self.semantic_scholar_abstract(doi)

    def enrich_missing_abstracts(self, papers: list[dict[str, Any]], progress=None, limit: int = 80) -> None:
        targets = [
            paper
            for paper in papers
            if not str(paper.get("abstract") or "").strip() and str(paper.get("doi") or "").strip()
        ][: max(0, limit)]
        if not targets:
            return
        filled = 0
        if progress:
            progress(f"补全摘要：正在从 Crossref / Semantic Scholar 尝试补齐 {len(targets)} 篇缺失摘要。")
        for index, paper in enumerate(targets, start=1):
            abstract = self.fallback_abstract(str(paper.get("doi") or ""))
            if abstract:
                paper["abstract"] = abstract
                filled += 1
            if progress and (index == len(targets) or index % 10 == 0):
                progress(f"补全摘要进度：{index}/{len(targets)}，已补齐 {filled} 篇。")
            time.sleep(0.03)
        if progress:
            progress(f"补全摘要完成：已补齐 {filled}/{len(targets)} 篇。")

    def search_title_quick(
        self,
        title: str,
        per_page: int = 8,
        filters: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        runtime_log.write(
            f"OpenAlex quick title search started. title={title}; per_page={per_page}; filters={filters or []}",
            "openalex",
        )
        clean_title = " ".join(str(title or "").replace(",", " ").split())
        filter_parts = [
            "type:article",
            "is_retracted:false",
            *(filters or []),
        ]
        params: dict[str, Any] = {
            "search": clean_title,
            "filter": ",".join(filter_parts),
            "per-page": max(1, min(per_page, 25)),
            "sort": "relevance_score:desc",
            "select": ",".join(
                [
                    "id",
                    "doi",
                    "title",
                    "authorships",
                    "abstract_inverted_index",
                    "publication_date",
                    "primary_location",
                    "best_oa_location",
                    "locations",
                    "open_access",
                    "is_retracted",
                    "relevance_score",
                    "cited_by_count",
                ]
            ),
        }
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key

        response = self.session.get(OPENALEX_WORKS_URL, params=params, timeout=8)
        if response.status_code == 403:
            raise RuntimeError("OpenAlex returned 403. Please set an OpenAlex API key and try again.")
        response.raise_for_status()
        data = response.json()
        candidates = [
            candidate
            for work in data.get("results", [])
            if (candidate := work_to_candidate(work, title))
        ]
        runtime_log.write(
            f"OpenAlex quick title search finished. title={title}; raw={len(data.get('results', []))}; kept={len(candidates)}",
            "openalex",
        )
        return candidates

    def search_publishers(self, query: str, per_page: int = 3) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": query, "per-page": max(1, min(per_page, 25))}
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        response = self.session.get(OPENALEX_PUBLISHERS_URL, params=params, timeout=12)
        response.raise_for_status()
        return list(response.json().get("results", []))

    def list_publisher_sources(self, publisher_id: str, per_page: int = 200) -> list[dict[str, Any]]:
        publisher_key = publisher_id.rsplit("/", 1)[-1]
        sources: list[dict[str, Any]] = []
        cursor = "*"
        while cursor:
            params: dict[str, Any] = {
                "filter": f"type:journal,is_oa:true,host_organization_lineage:{publisher_key}",
                "per-page": max(1, min(per_page, 200)),
                "cursor": cursor,
            }
            if self.mailto:
                params["mailto"] = self.mailto
            if self.api_key:
                params["api_key"] = self.api_key
            response = self.session.get(OPENALEX_SOURCES_URL, params=params, timeout=12)
            response.raise_for_status()
            data = response.json()
            batch = list(data.get("results", []))
            sources.extend(batch)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not batch:
                break
        return sources


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def rank_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not candidates:
        return []

    deduped: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = (item.get("doi") or item.get("id") or item.get("pdf_url") or "").lower()
        if not key:
            continue
        old = deduped.get(key)
        if not old or item.get("relevance_score", 0) > old.get("relevance_score", 0):
            deduped[key] = item

    values = list(deduped.values())
    max_rel = max((item.get("relevance_score") or 0 for item in values), default=1) or 1
    today = date.today()

    for item in values:
        rel = (item.get("relevance_score") or 0) / max_rel
        pub_date = item.get("publication_date") or "1900-01-01"
        try:
            year, month, day = [int(part) for part in pub_date.split("-")[:3]]
            age_days = max(0, (today - date(year, month, day)).days)
        except Exception:
            age_days = 36500
        recency = math.exp(-age_days / 1825)
        item["_rank_score"] = rel * 0.65 + recency * 0.35

    return sorted(values, key=lambda x: x.get("_rank_score", 0), reverse=True)[:limit]


def collect_candidates(
    client: OpenAlexClient,
    queries: list[str],
    target: int,
    progress,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen = set()
    per_query = 50 if target <= 300 else 100

    for index, query in enumerate(queries, start=1):
        progress(f"Search group {index}/{len(queries)}: {query}")
        for sort in ("relevance_score:desc", "publication_date:desc"):
            try:
                results = client.search(query, per_page=per_query, sort=sort)
            except Exception as exc:
                progress(f"Search failed: {query}; reason: {exc}")
                continue
            for item in results:
                key = (item.get("doi") or item.get("id") or item.get("pdf_url") or "").lower()
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(item)
            time.sleep(0.15)
        if len(candidates) >= target * 3:
            break

    progress(f"Collected {len(candidates)} candidates; ranking by relevance and publication date.")
    return rank_candidates(candidates, target)
