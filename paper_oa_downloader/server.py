from __future__ import annotations

import csv
import base64
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import runtime_log
from .downloader import PdfDownloader, reusable_pdf_path, safe_filename, unique_pdf_path
from .oa_sources import FAMOUS_OA_PUBLISHERS, OA_PUBLISHER_SEARCH_TERMS, clean_keywords, pair_queries, single_queries
from .openalex import OpenAlexClient, chunked, rank_candidates
from .quartiles import QuartileLookup
from .storage import Storage


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


class SearchRequest(BaseModel):
    keywords: list[str] = Field(default_factory=list, max_length=3)
    title: str = ""
    mode: str = "keywords"
    max_papers: int = Field(default=100, ge=1, le=1000)
    openalex_api_key: str = ""
    mailto: str = ""


class AddRepositoryRequest(BaseModel):
    search_job_id: str
    paper_ids: list[str] = Field(default_factory=list)


class DownloadOneRequest(BaseModel):
    search_job_id: str = ""
    paper_id: str


class ImportPdfRequest(BaseModel):
    search_job_id: str = ""
    paper_id: str
    pdf_path: str


class ProjectRequest(BaseModel):
    name: str


class AppSettingsRequest(BaseModel):
    handoff_first_action_seconds: int = Field(default=10)


class JobState:
    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.status = "queued"
        self.total = 0
        self.target = 0
        self.searched = 0
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
        self.pause_requested = False
        self.logs: list[str] = []
        self.error = ""
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = ""
        self.lock = threading.Lock()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(f"{time.strftime('%H:%M:%S')} {message}")
            self.logs = self.logs[-240:]
        runtime_log.write(f"{self.kind} job {self.id}: {message}", "job")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "total": self.total,
                "target": self.target,
                "searched": self.searched,
                "downloaded": self.downloaded,
                "failed": self.failed,
                "skipped": self.skipped,
                "logs": list(self.logs),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


def create_sample_quartile_csv(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["issn_l", "journal_title", "sci_quartile", "source", "note"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "issn_l": "1932-6203",
                "journal_title": "PLOS ONE",
                "sci_quartile": "Example: Q1/Q2",
                "source": "Replace this with your licensed JCR/CAS data",
                "note": "The app matches local data only and does not bundle restricted quartile data",
            }
        )


def candidate_key(item: dict[str, Any]) -> str:
    return (item.get("doi") or item.get("id") or item.get("pdf_url") or "").lower()


def paper_public_view(paper: dict[str, Any]) -> dict[str, Any]:
    hidden = {"issn_l", "issns", "_rank_score"}
    return {key: value for key, value in paper.items() if key not in hidden}


def plain_search_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).casefold()


def paper_matches_keywords(paper: dict[str, Any], keywords: list[str]) -> bool:
    haystack = plain_search_text(f"{paper.get('title') or ''} {paper.get('abstract') or ''}")
    return any(plain_search_text(keyword) in haystack for keyword in keywords if plain_search_text(keyword))


def title_tokens(value: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "as",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", plain_search_text(value))
        if len(token) > 2 and token not in stopwords
    ]


def paper_matches_title(paper: dict[str, Any], title: str) -> bool:
    wanted = plain_search_text(title)
    candidate = plain_search_text(paper.get("title") or "")
    if not wanted or not candidate:
        return False
    if wanted in candidate or candidate in wanted:
        return True
    tokens = title_tokens(title)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in candidate)
    return hits >= max(2, int(len(tokens) * 0.72))


def compact_title_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", plain_search_text(value)))


def paper_exactly_matches_title(paper: dict[str, Any], title: str) -> bool:
    wanted = compact_title_key(title)
    candidate = compact_title_key(str(paper.get("title") or ""))
    if not wanted or not candidate:
        return False
    if wanted == candidate:
        return True
    wanted_tokens = wanted.split()
    candidate_tokens = candidate.split()
    if len(wanted_tokens) < 5 or len(candidate_tokens) < 5:
        return False
    shared = sum(1 for token in wanted_tokens if token in candidate_tokens)
    return shared >= max(5, int(len(wanted_tokens) * 0.92)) and abs(len(wanted_tokens) - len(candidate_tokens)) <= 2


def normalize_repo_id(repo_id: str) -> str:
    if repo_id not in {"repo1", "repo2", "repo3"}:
        raise HTTPException(404, "Repository not found.")
    return repo_id


def create_app(root: Path) -> FastAPI:
    runtime_log.write(f"FastAPI app created. root={root}", "server")
    default_data_dir = root / "data"
    default_downloads_dir = root / "downloads"
    reserved_project_dirs = {
        ".agents",
        ".codex",
        ".git",
        ".venv",
        "__pycache__",
        "artifacts",
        "data",
        "downloads",
        "paper_oa_downloader",
        "projects",
        "ui",
    }
    project_state_path = default_data_dir / "current_project.json"
    app_settings_path = default_data_dir / "settings.json"
    ui_dir = root / "ui"
    quartile_path = default_data_dir / "journal_quartiles.csv"
    create_sample_quartile_csv(quartile_path)
    quartiles = QuartileLookup(quartile_path)
    oa_sources_cache_path = default_data_dir / "oa_publisher_sources.json"
    active_project_id: str | None = None
    jobs: dict[str, JobState] = {}
    search_results: dict[str, list[dict[str, Any]]] = {}

    app = FastAPI(title="OA PDF Downloader")

    def normalize_handoff_seconds(value: Any) -> int:
        try:
            seconds = int(value)
        except Exception:
            seconds = 10
        return seconds if seconds in {0, 10, 20, 30, 60} else 10

    def load_app_settings() -> dict[str, Any]:
        try:
            if app_settings_path.exists():
                data = json.loads(app_settings_path.read_text(encoding="utf-8"))
            else:
                data = {}
        except Exception as exc:
            runtime_log.write(f"App settings ignored: {exc}", "settings")
            data = {}
        return {
            "handoff_first_action_seconds": normalize_handoff_seconds(
                data.get("handoff_first_action_seconds", 10)
            )
        }

    def save_app_settings(settings: dict[str, Any]) -> None:
        default_data_dir.mkdir(parents=True, exist_ok=True)
        app_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def json_response(payload: Any) -> Response:
        return Response(
            json.dumps(payload, ensure_ascii=True),
            media_type="application/json",
        )

    def project_id_from_name(name: str) -> str:
        raw = str(name or "").strip()
        try:
            selected_path = Path(raw)
            if selected_path.is_absolute():
                resolved = selected_path.resolve()
                if resolved.parent != root.resolve():
                    raise HTTPException(400, "请选择程序目录下的项目文件夹。")
                cleaned = safe_filename(resolved.name, "project")
            else:
                cleaned = safe_filename(raw, "project")
        except HTTPException:
            raise
        except Exception:
            cleaned = safe_filename(raw, "project")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned or cleaned.casefold() in {"default", "默认项目"}:
            raise HTTPException(400, "请先新建或选择一个项目文件夹。")
        return cleaned

    def validate_project_id(project_id: str) -> None:
        if project_id.casefold() in reserved_project_dirs:
            raise HTTPException(400, "该名称是程序保留文件夹，请换一个项目名。")

    def project_label(project_id: str | None) -> str:
        return project_id or ""

    def project_root(project_id: str | None = None) -> Path:
        selected = project_id if project_id is not None else active_project_id
        if not selected:
            raise HTTPException(400, "请先新建项目。")
        return root / selected

    def project_paths(project_id: str | None = None) -> tuple[Path, Path]:
        base = project_root(project_id)
        return base / "data", base / "downloads"

    def ensure_project(project_id: str) -> None:
        validate_project_id(project_id)
        data_path, downloads_path = project_paths(project_id)
        data_path.mkdir(parents=True, exist_ok=True)
        downloads_path.mkdir(parents=True, exist_ok=True)
        Storage(data_path / "papers.sqlite3")

    def project_db() -> Storage:
        data_path, _ = project_paths()
        return Storage(data_path / "papers.sqlite3")

    def repositories_path() -> Path:
        data_path, _ = project_paths()
        return data_path / "repositories.json"

    def downloads_dir() -> Path:
        _, path = project_paths()
        return path

    def save_active_project() -> None:
        default_data_dir.mkdir(parents=True, exist_ok=True)
        project_state_path.write_text(
            json.dumps({"active": active_project_id or ""}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_active_project() -> None:
        nonlocal active_project_id
        try:
            if project_state_path.exists():
                data = json.loads(project_state_path.read_text(encoding="utf-8"))
                raw_active = str(data.get("active") or "").strip()
                if raw_active and raw_active.casefold() not in {"default", "默认项目"}:
                    active_project_id = project_id_from_name(raw_active)
        except Exception as exc:
            runtime_log.write(f"Active project state ignored: {exc}", "project")
            active_project_id = None
        if active_project_id:
            ensure_project(active_project_id)

    load_active_project()

    @app.middleware("http")
    async def log_api_requests(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            start = time.perf_counter()
            runtime_log.write(f"Request started: {request.method} {request.url.path}", "api")
            try:
                response = await call_next(request)
            except Exception as exc:
                elapsed = int((time.perf_counter() - start) * 1000)
                runtime_log.write(
                    f"Request failed: {request.method} {request.url.path}; "
                    f"elapsed={elapsed}ms; error={exc}",
                    "api",
                )
                raise
            elapsed = int((time.perf_counter() - start) * 1000)
            runtime_log.write(
                f"Request finished: {request.method} {request.url.path}; "
                f"status={response.status_code}; elapsed={elapsed}ms",
                "api",
            )
            return response
        return await call_next(request)

    def load_repositories() -> dict[str, list[dict[str, Any]]]:
        runtime_log.write("Loading repositories.", "storage")
        path = repositories_path()
        if not path.exists():
            return {"repo1": [], "repo2": [], "repo3": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        return {
            "repo1": list(data.get("repo1") or []),
            "repo2": list(data.get("repo2") or []),
            "repo3": list(data.get("repo3") or []),
        }

    def save_repositories(data: dict[str, list[dict[str, Any]]]) -> None:
        runtime_log.write(
            f"Saving repositories: repo1={len(data.get('repo1') or [])}, "
            f"repo2={len(data.get('repo2') or [])}, repo3={len(data.get('repo3') or [])}.",
            "storage",
        )
        path = repositories_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_oa_publisher_source_ids(request: SearchRequest, job: JobState) -> list[str]:
        try:
            if oa_sources_cache_path.exists():
                cached = json.loads(oa_sources_cache_path.read_text(encoding="utf-8"))
                if time.time() - float(cached.get("created_at") or 0) < 7 * 24 * 3600:
                    ids = [str(item) for item in cached.get("source_ids") or [] if item]
                    if ids:
                        job.log(f"Using cached OA publisher journal list: {len(ids)} sources.")
                        return ids
        except Exception as exc:
            job.log(f"OA publisher source cache ignored: {exc}")

        client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
        publisher_ids: dict[str, str] = {}
        source_ids: dict[str, str] = {}
        job.log("Refreshing OA publisher journal list from OpenAlex.")
        for term in OA_PUBLISHER_SEARCH_TERMS:
            try:
                publishers = client.search_publishers(term, per_page=3)
            except Exception as exc:
                job.log(f"Publisher lookup failed: {term}; {exc}")
                continue
            if not publishers:
                continue
            publisher = publishers[0]
            publisher_id = str(publisher.get("id") or "")
            publisher_name = str(publisher.get("display_name") or term)
            if not publisher_id or publisher_id in publisher_ids:
                continue
            publisher_ids[publisher_id] = publisher_name
            try:
                sources = client.list_publisher_sources(publisher_id)
            except Exception as exc:
                job.log(f"Source lookup failed: {publisher_name}; {exc}")
                continue
            for source in sources:
                source_id = str(source.get("id") or "").rsplit("/", 1)[-1]
                if source_id:
                    source_ids[source_id] = str(source.get("display_name") or source_id)
            job.log(f"  {publisher_name}: {len(sources)} OA journals.")
            time.sleep(0.05)

        ids = sorted(source_ids)
        try:
            oa_sources_cache_path.write_text(
                json.dumps(
                    {
                        "created_at": time.time(),
                        "publisher_ids": publisher_ids,
                        "source_ids": ids,
                        "source_count": len(ids),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            job.log(f"OA publisher source cache write failed: {exc}")
        job.log(f"OA publisher journal list ready: {len(ids)} sources.")
        return ids

    def load_oa_publisher_ids(request: SearchRequest, job: JobState) -> list[str]:
        try:
            if oa_sources_cache_path.exists():
                cached = json.loads(oa_sources_cache_path.read_text(encoding="utf-8"))
                if time.time() - float(cached.get("created_at") or 0) < 7 * 24 * 3600:
                    ids = [str(item).rsplit("/", 1)[-1] for item in (cached.get("publisher_ids") or {}).keys()]
                    ids = sorted({item for item in ids if item})
                    if ids:
                        job.log(f"Using cached OA publisher list: {len(ids)} publishers.")
                        return ids
        except Exception as exc:
            job.log(f"OA publisher cache ignored: {exc}")

        client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
        publisher_ids: dict[str, str] = {}
        job.log("Refreshing OA publisher list from OpenAlex.")
        for term in OA_PUBLISHER_SEARCH_TERMS:
            try:
                publishers = client.search_publishers(term, per_page=3)
            except Exception as exc:
                job.log(f"Publisher lookup failed: {term}; {exc}")
                continue
            if not publishers:
                continue
            publisher = publishers[0]
            publisher_id = str(publisher.get("id") or "")
            publisher_name = str(publisher.get("display_name") or term)
            if publisher_id:
                publisher_ids[publisher_id] = publisher_name
            time.sleep(0.02)

        ids = sorted({item.rsplit("/", 1)[-1] for item in publisher_ids if item})
        try:
            cached = {}
            if oa_sources_cache_path.exists():
                cached = json.loads(oa_sources_cache_path.read_text(encoding="utf-8"))
            cached.update(
                {
                    "created_at": time.time(),
                    "publisher_ids": publisher_ids,
                    "source_ids": cached.get("source_ids") or [],
                    "source_count": len(cached.get("source_ids") or []),
                }
            )
            oa_sources_cache_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            job.log(f"OA publisher cache write failed: {exc}")
        job.log(f"OA publisher list ready: {len(ids)} publishers.")
        return ids

    def downloaded_pdf_map() -> dict[str, str]:
        runtime_log.write("Scanning downloaded PDFs.", "storage")
        paths: dict[str, str] = {}
        for item in project_db().list_papers(limit=10000):
            path = Path(item.get("pdf_path") or "")
            if not path.exists():
                continue
            for key in {
                candidate_key(item),
                (item.get("id") or "").rstrip("/").split("/")[-1].lower(),
                f"title:{safe_filename(item.get('title') or '').casefold()}",
            }:
                if key:
                    paths[key] = str(path)
        for path in downloads_dir().rglob("*.pdf"):
            if path.exists() and path.stat().st_size > 1024:
                paths.setdefault(f"title:{path.stem.casefold()}", str(path))
        return paths

    def attach_downloaded_pdf(paper: dict[str, Any], pdf_paths: dict[str, str] | None = None) -> dict[str, Any]:
        pdf_paths = pdf_paths or downloaded_pdf_map()
        for key in {
            candidate_key(paper),
            (paper.get("id") or "").rstrip("/").split("/")[-1].lower(),
            f"title:{safe_filename(paper.get('title') or '').casefold()}",
        }:
            if key and key in pdf_paths:
                paper["pdf_path"] = pdf_paths[key]
                break
        return paper

    def public_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pdf_paths = downloaded_pdf_map()
        return [paper_public_view(attach_downloaded_pdf(dict(item), pdf_paths)) for item in papers]

    def find_known_paper(paper_id: str, search_job_id: str = "") -> dict[str, Any] | None:
        key = paper_id.lower()
        sources: list[list[dict[str, Any]]] = []
        if search_job_id:
            sources.append(search_results.get(search_job_id, []))
        repositories = load_repositories()
        sources.extend(repositories.values())
        sources.append(project_db().list_papers(limit=10000))
        for papers in sources:
            for item in papers:
                if candidate_key(item) == key or (item.get("id") or "").rstrip("/").split("/")[-1].lower() == key:
                    return attach_downloaded_pdf(dict(item))
        return None

    def find_pdf_path_for_paper_id(paper_id: str) -> Path | None:
        key = paper_id.lower()
        sources: list[list[dict[str, Any]]] = [
            project_db().list_papers(limit=10000),
            *load_repositories().values(),
        ]
        for papers in sources:
            for item in papers:
                item_id = str(item.get("id") or "")
                item_key = candidate_key(item)
                item_short_id = item_id.rstrip("/").split("/")[-1].lower()
                if item_short_id == key or item_id.lower() == key or item_key == key:
                    paper = attach_downloaded_pdf(dict(item))
                    path = Path(paper.get("pdf_path") or "")
                    if path.is_file() and path.suffix.lower() == ".pdf":
                        return path
        return None

    def ensure_paper_in_repository(repo_id: str, paper: dict[str, Any]) -> list[dict[str, Any]]:
        repositories = load_repositories()
        repo = repositories[repo_id]
        key = candidate_key(paper)
        for index, item in enumerate(repo):
            if candidate_key(item) == key:
                merged = dict(item)
                merged.update({k: v for k, v in paper.items() if v})
                repo[index] = merged
                save_repositories(repositories)
                return repo
        repo.append(dict(paper))
        save_repositories(repositories)
        return repo

    def add_results(
        client: OpenAlexClient,
        query: str,
        candidates: list[dict[str, Any]],
        seen: set[str],
        job: JobState,
        keywords: list[str],
    ) -> None:
        job.log(f"Search query: {query}")
        for sort in ("relevance_score:desc", "publication_date:desc"):
            try:
                results = client.search(query, per_page=100, sort=sort)
            except Exception as exc:
                job.log(f"Search failed: {query}; {exc}")
                continue
            added = 0
            skipped_keyword = 0
            skipped_duplicate = 0
            for item in results:
                key = candidate_key(item)
                if not key:
                    continue
                if key in seen:
                    skipped_duplicate += 1
                    continue
                seen.add(key)
                if not paper_matches_keywords(item, keywords):
                    skipped_keyword += 1
                    continue
                candidates.append(item)
                added += 1
            job.searched = len(candidates)
            job.log(
                f"  {sort}: +{added}, valid {len(candidates)}; "
                f"skipped {skipped_keyword} without keyword in title/abstract, "
                f"{skipped_duplicate} duplicate"
            )
            time.sleep(0.15)

    def fetch_query_results(
        query: str,
        keywords: list[str],
        request: SearchRequest,
        sort: str,
        per_page: int,
        filters: list[str] | None = None,
    ) -> tuple[str, str, list[dict[str, Any]], int]:
        client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
        results = client.search(query, per_page=per_page, sort=sort, filters=filters)
        kept = [
            item
            for item in results
            if paper_matches_keywords(item, keywords)
        ]
        skipped = len(results) - len(kept)
        return query, sort, kept, skipped

    def collect_query_batch(
        queries: list[str],
        keywords: list[str],
        request: SearchRequest,
        candidates: list[dict[str, Any]],
        seen: set[str],
        job: JobState,
        sort: str,
        filters: list[str] | None = None,
    ) -> None:
        if not queries:
            return
        per_page = 100 if request.max_papers <= 100 else 150 if request.max_papers <= 300 else 200
        workers = min(5, len(queries))
        job.log(f"Search batch: {len(queries)} queries, sort {sort}, {workers} workers, filters {filters or []}.")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(fetch_query_results, query, keywords, request, sort, per_page, filters)
                for query in queries
            ]
            for future in as_completed(futures):
                try:
                    query, used_sort, results, skipped_keyword = future.result()
                except Exception as exc:
                    job.log(f"Search failed: {exc}")
                    continue
                added = 0
                skipped_duplicate = 0
                for item in results:
                    key = candidate_key(item)
                    if not key:
                        continue
                    if key in seen:
                        skipped_duplicate += 1
                        continue
                    seen.add(key)
                    candidates.append(item)
                    added += 1
                job.searched = min(len(candidates), request.max_papers)
                job.log(
                    f"  {query} / {used_sort}: +{added}, valid {len(candidates)}; "
                    f"skipped {skipped_keyword} keyword miss, {skipped_duplicate} duplicate"
                )

    def collect_oa_publisher_source_batch(
        queries: list[str],
        keywords: list[str],
        request: SearchRequest,
        candidates: list[dict[str, Any]],
        seen: set[str],
        job: JobState,
    ) -> None:
        target_pool = request.max_papers
        if not queries or len(candidates) >= target_pool:
            return
        source_ids = load_oa_publisher_source_ids(request, job)
        if not source_ids:
            return
        source_chunks = chunked(source_ids, 35)
        per_page = 100 if request.max_papers <= 100 else 150 if request.max_papers <= 300 else 200
        query_budget = max(1, min(len(queries), 3))
        job.log(
            f"OA publisher journal pass: {len(source_ids)} sources in {len(source_chunks)} groups; "
            f"{query_budget} query groups."
        )
        tasks = [
            (query, group_index, source_group)
            for query in queries[:query_budget]
            for group_index, source_group in enumerate(source_chunks, start=1)
        ]

        def fetch_oa_source_group(task: tuple[str, int, list[str]]) -> tuple[str, int, list[dict[str, Any]], str]:
            query, group_index, source_group = task
            client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
            try:
                results = client.search(
                    query,
                    per_page=per_page,
                    sort="relevance_score:desc",
                    filters=[f"locations.source.id:{'|'.join(source_group)}"],
                )
                return query, group_index, results, ""
            except Exception as exc:
                return query, group_index, [], str(exc)

        pool = ThreadPoolExecutor(max_workers=min(8, len(tasks)))
        futures = [pool.submit(fetch_oa_source_group, task) for task in tasks]
        try:
            for future in as_completed(futures):
                if len(candidates) >= target_pool:
                    for pending in futures:
                        pending.cancel()
                    break
                query, group_index, results, error = future.result()
                if error:
                    job.log(f"OA publisher source pass failed: {query}; group {group_index}; {error}")
                    continue
                added = 0
                skipped_keyword = 0
                skipped_duplicate = 0
                for item in results:
                    if len(candidates) >= target_pool:
                        break
                    key = candidate_key(item)
                    if not key:
                        continue
                    if key in seen:
                        skipped_duplicate += 1
                        continue
                    if not paper_matches_keywords(item, keywords):
                        skipped_keyword += 1
                        continue
                    seen.add(key)
                    candidates.append(item)
                    added += 1
                job.searched = min(len(candidates), request.max_papers)
                if added:
                    job.log(
                        f"  OA sources {group_index}/{len(source_chunks)} for {query}: "
                        f"+{added}, valid {len(candidates)}; skipped {skipped_keyword} keyword miss, "
                        f"{skipped_duplicate} duplicate"
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def collect_oa_publisher_batch(
        queries: list[str],
        keywords: list[str],
        request: SearchRequest,
        candidates: list[dict[str, Any]],
        seen: set[str],
        job: JobState,
    ) -> None:
        target_pool = request.max_papers
        if not queries or len(candidates) >= target_pool:
            return
        publisher_ids = load_oa_publisher_ids(request, job)
        if not publisher_ids:
            collect_oa_publisher_source_batch(queries, keywords, request, candidates, seen, job)
            return

        publisher_chunks = chunked(publisher_ids, 8)
        per_page = 100 if request.max_papers <= 100 else 150 if request.max_papers <= 300 else 200
        query_budget = max(1, min(len(queries), 2))
        job.log(
            f"Fast OA publisher pass: {len(publisher_ids)} publishers in {len(publisher_chunks)} groups; "
            f"{query_budget} query groups."
        )
        tasks = [
            (query, group_index, publisher_group)
            for query in queries[:query_budget]
            for group_index, publisher_group in enumerate(publisher_chunks, start=1)
        ]

        def fetch_publisher_group(task: tuple[str, int, list[str]]) -> tuple[str, int, list[dict[str, Any]], str]:
            query, group_index, publisher_group = task
            client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
            try:
                results = client.search(
                    query,
                    per_page=per_page,
                    sort="relevance_score:desc",
                    filters=[
                        "open_access.is_oa:true",
                        f"primary_location.source.publisher_lineage:{'|'.join(publisher_group)}",
                    ],
                )
                return query, group_index, results, ""
            except Exception as exc:
                return query, group_index, [], str(exc)

        pool = ThreadPoolExecutor(max_workers=min(8, len(tasks)))
        futures = [pool.submit(fetch_publisher_group, task) for task in tasks]
        try:
            for future in as_completed(futures):
                if len(candidates) >= target_pool:
                    for pending in futures:
                        pending.cancel()
                    break
                query, group_index, results, error = future.result()
                if error:
                    job.log(f"Fast OA publisher pass failed: {query}; group {group_index}; {error}")
                    continue
                added = 0
                skipped_keyword = 0
                skipped_duplicate = 0
                for item in results:
                    if len(candidates) >= target_pool:
                        break
                    key = candidate_key(item)
                    if not key:
                        continue
                    if key in seen:
                        skipped_duplicate += 1
                        continue
                    if not paper_matches_keywords(item, keywords):
                        skipped_keyword += 1
                        continue
                    seen.add(key)
                    candidates.append(item)
                    added += 1
                job.searched = min(len(candidates), request.max_papers)
                if added:
                    job.log(
                        f"  OA publishers {group_index}/{len(publisher_chunks)} for {query}: "
                        f"+{added}, valid {len(candidates)}; skipped {skipped_keyword} keyword miss, "
                        f"{skipped_duplicate} duplicate"
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if not candidates:
            job.log("Fast OA publisher pass returned no papers; falling back to OA journal source groups.")
            collect_oa_publisher_source_batch(queries, keywords, request, candidates, seen, job)

    def run_search_job(job: JobState, request: SearchRequest) -> None:
        job.status = "searching"
        job.target = request.max_papers
        try:
            keywords = clean_keywords(request.keywords)
            pairs = pair_queries(keywords)
            singles = single_queries(keywords)
            job.log(f"Keywords: {', '.join(keywords)}")
            job.log(f"Max papers: {request.max_papers}; fewer papers may be shown if fewer matches exist.")
            job.log("Search is restricted to journals under major OA publishers for better PDF availability.")
            job.log(f"Pair queries: {len(pairs)}; single queries: {len(singles)}")

            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()

            queries = []
            for query in [*pairs, *singles]:
                if query not in queries:
                    queries.append(query)
            if not queries:
                raise RuntimeError("No valid keyword queries were generated.")
            collect_oa_publisher_batch(queries, keywords, request, candidates, seen, job)

            ranked = rank_candidates(candidates, request.max_papers)
            if len(ranked) < request.max_papers:
                job.log(
                    f"OA publisher journal search found {len(ranked)}/{request.max_papers}; "
                    "using OpenAlex OA-wide supplement."
                )
                collect_query_batch(
                    queries,
                    keywords,
                    request,
                    candidates,
                    seen,
                    job,
                    "relevance_score:desc",
                    filters=["open_access.is_oa:true"],
                )

            ranked = rank_candidates(candidates, request.max_papers)
            if len(ranked) < request.max_papers:
                job.log("OA-wide supplement is still sparse; adding recent OA papers from OpenAlex.")
                collect_query_batch(
                    queries,
                    keywords,
                    request,
                    candidates,
                    seen,
                    job,
                    "publication_date:desc",
                    filters=["open_access.is_oa:true"],
                )

            final = rank_candidates(candidates, request.max_papers)
            pdf_paths = downloaded_pdf_map()
            for paper in final:
                paper["sci_quartile"] = quartiles.lookup(
                    paper.get("journal"),
                    paper.get("issn_l"),
                    paper.get("issns"),
                )
                paper["pdf_path"] = ""
                attach_downloaded_pdf(paper, pdf_paths)

            search_results[job.id] = final
            job.total = len(final)
            job.searched = len(final)
            job.status = "ready"
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"Search ready: showing {len(final)} papers, upper limit {request.max_papers}.")
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"Search job error: {exc}")

    def run_title_search_job(job: JobState, request: SearchRequest) -> None:
        job.status = "searching"
        job.target = request.max_papers
        try:
            title = request.title.strip()
            client = OpenAlexClient(api_key=request.openalex_api_key, mailto=request.mailto)
            job.log(f"Title search: {title}")
            job.log(f"Max papers: {request.max_papers}; fewer papers may be shown if fewer matches exist.")
            job.log("Title search uses OpenAlex OA-wide matching first, because the journal may not be under the configured OA publishers.")

            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            per_page = 100 if request.max_papers <= 100 else 150 if request.max_papers <= 300 else 200

            def add_title_results(label: str, results: list[dict[str, Any]]) -> None:
                added = 0
                skipped_title = 0
                skipped_duplicate = 0
                for item in results:
                    if len(candidates) >= request.max_papers:
                        break
                    key = candidate_key(item)
                    if not key:
                        continue
                    if key in seen:
                        skipped_duplicate += 1
                        continue
                    if not paper_matches_title(item, title):
                        skipped_title += 1
                        continue
                    seen.add(key)
                    candidates.append(item)
                    added += 1
                job.searched = min(len(candidates), request.max_papers)
                job.log(
                    f"  {label}: +{added}, valid {len(candidates)}; "
                    f"skipped {skipped_title} title miss, {skipped_duplicate} duplicate"
                )

            def finish_title_search(final: list[dict[str, Any]], label: str) -> None:
                pdf_paths = downloaded_pdf_map()
                for paper in final:
                    paper["sci_quartile"] = quartiles.lookup(
                        paper.get("journal"),
                        paper.get("issn_l"),
                        paper.get("issns"),
                    )
                    paper["pdf_path"] = ""
                    attach_downloaded_pdf(paper, pdf_paths)

                search_results[job.id] = final
                job.total = len(final)
                job.searched = len(final)
                job.status = "ready"
                job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                job.log(f"{label}: showing {len(final)} papers, upper limit {request.max_papers}.")

            try:
                quick_results = client.search_title_quick(title, per_page=8)
                add_title_results("OpenAlex quick title search", quick_results)
                exact_matches = [paper for paper in rank_candidates(candidates, request.max_papers) if paper_exactly_matches_title(paper, title)]
                if exact_matches:
                    finish_title_search(exact_matches, "Fast exact title search ready")
                    return
            except Exception as exc:
                job.log(f"OpenAlex quick title search failed; falling back to full title search: {exc}")

            for sort in ("relevance_score:desc", "publication_date:desc"):
                if len(candidates) >= request.max_papers:
                    break
                try:
                    results = client.search_title(
                        title,
                        per_page=per_page,
                        sort=sort,
                        filters=["open_access.is_oa:true"],
                    )
                except Exception as exc:
                    job.log(f"OpenAlex OA-wide title.search failed: {sort}; {exc}")
                    continue
                add_title_results(f"OpenAlex OA-wide title.search / {sort}", results)

            ranked = rank_candidates(candidates, request.max_papers)
            if len(ranked) < request.max_papers:
                job.log(
                    f"OpenAlex title.search found {len(ranked)}/{request.max_papers}; "
                    "using broader OpenAlex OA title supplement."
                )
                for sort in ("relevance_score:desc", "publication_date:desc"):
                    if len(rank_candidates(candidates, request.max_papers)) >= request.max_papers:
                        break
                    try:
                        results = client.search(
                            title,
                            per_page=per_page,
                            sort=sort,
                            filters=["open_access.is_oa:true"],
                        )
                    except Exception as exc:
                        job.log(f"OpenAlex OA-wide title supplement failed: {sort}; {exc}")
                        continue
                    add_title_results(f"OpenAlex OA-wide broad title / {sort}", results)

            final = rank_candidates(candidates, request.max_papers)
            finish_title_search(final, "Title search ready")
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"Title search job error: {exc}")

    def run_download_job(job: JobState, papers: list[dict[str, Any]], repo_id: str | None = None) -> None:
        job.status = "downloading"
        try:
            if not papers:
                raise RuntimeError("No repository papers to download.")
            job.total = len(papers)
            job.target = len(papers)
            base_downloads_dir = downloads_dir()
            target_dir = base_downloads_dir / repo_id if repo_id else base_downloads_dir

            def download_one(index: int, paper: dict[str, Any]) -> tuple[int, dict[str, Any], Path | None, str]:
                try:
                    existing = reusable_pdf_path(paper.get("pdf_path"))
                    if existing:
                        return index, paper, existing, "SKIP_EXISTING_LOCAL"
                    if not paper.get("pdf_url"):
                        return index, paper, None, "SKIP_NO_SOURCE"
                    settings = load_app_settings()
                    path = PdfDownloader(
                        target_dir,
                        handoff_first_action_seconds=settings["handoff_first_action_seconds"],
                    ).download(paper)
                    updated = dict(paper)
                    updated["pdf_path"] = str(path)
                    project_db().upsert_paper(updated)
                    return index, updated, path, ""
                except Exception as exc:
                    return index, paper, None, str(exc)

            job.log(f"Starting repository download: {len(papers)} papers. Using a gentle one-by-one mode to reduce verification triggers.")
            updated_papers = [dict(paper) for paper in papers]
            for index, paper in enumerate(papers, start=1):
                if job.pause_requested:
                    job.status = "paused"
                    job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    job.log("Download paused by user.")
                    break
                index, updated, path, error = download_one(index, paper)
                updated_papers[index - 1] = updated
                if error in {"SKIP_EXISTING_LOCAL", "SKIP_NO_SOURCE"}:
                    with job.lock:
                        job.skipped += 1
                    reason = "已有本地文件" if error == "SKIP_EXISTING_LOCAL" else "没有 PDF 源"
                    job.log(f"Skipped {index}/{len(papers)}: {updated.get('title')}; reason: {reason}")
                elif error:
                    with job.lock:
                        job.failed += 1
                    job.log(f"Download failed {index}/{len(papers)}: {updated.get('title')}; reason: {error}")
                else:
                    with job.lock:
                        job.downloaded += 1
                    job.log(f"Saved {index}/{len(papers)}: {updated.get('title')}")
                if index < len(papers):
                    time.sleep(2.5)

            if job.status != "paused":
                job.status = "finished"
                job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"Download finished: {job.downloaded} succeeded, {job.failed} failed, {job.skipped} skipped.")
            if repo_id:
                repositories = load_repositories()
                repo = repositories[repo_id]
                positions = {candidate_key(item): index for index, item in enumerate(repo)}
                for paper in updated_papers:
                    key = candidate_key(paper)
                    if key in positions:
                        repo[positions[key]] = paper
                    else:
                        repo.append(paper)
                save_repositories(repositories)
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"Download job stopped: {exc}")

    def run_manual_pdf_job(job: JobState, paper: dict[str, Any], repo_id: str) -> None:
        job.status = "manual_pdf"
        try:
            job.total = 1
            job.target = 1
            target_dir = downloads_dir() / repo_id
            job.log(f"Opening and saving source PDF: {paper.get('title')}")
            settings = load_app_settings()
            path = PdfDownloader(
                target_dir,
                handoff_first_action_seconds=settings["handoff_first_action_seconds"],
            ).download_with_visible_browser(paper)
            updated = dict(paper)
            updated["pdf_path"] = str(path)
            project_db().upsert_paper(updated)

            repositories = load_repositories()
            repo = repositories[repo_id]
            key = candidate_key(updated)
            for index, item in enumerate(repo):
                if candidate_key(item) == key:
                    repo[index] = updated
                    break
            else:
                repo.append(updated)
            save_repositories(repositories)

            job.downloaded = 1
            job.status = "finished"
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.log(f"PDF saved to repository: {path.name}")
        except Exception as exc:
            if "SKIP_HUMAN_VERIFICATION" in str(exc):
                job.failed = 0
                job.status = "finished"
                job.error = ""
                job.log("Human verification was required; skipped this paper.")
            else:
                job.failed = 1
                job.status = "error"
                job.error = str(exc)
                job.log(f"Manual PDF download failed: {exc}")
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

    @app.get("/api/publishers")
    def publishers() -> list[dict[str, str]]:
        return [asdict(item) for item in FAMOUS_OA_PUBLISHERS]

    @app.post("/api/search")
    def search(request: SearchRequest) -> dict[str, str]:
        mode = request.mode.strip().casefold()
        if mode == "title":
            if not request.title.strip():
                raise HTTPException(400, "Article title is required.")
            job = JobState(str(uuid.uuid4()), "search")
            jobs[job.id] = job
            runtime_log.write(
                f"Title search requested. job={job.id}; title={request.title.strip()}; max={request.max_papers}",
                "api",
            )
            thread = threading.Thread(target=run_title_search_job, args=(job, request), daemon=True)
            thread.start()
            return {"job_id": job.id}

        cleaned = clean_keywords(request.keywords)
        if not cleaned:
            raise HTTPException(400, "At least one keyword is required.")
        job = JobState(str(uuid.uuid4()), "search")
        jobs[job.id] = job
        request.keywords = cleaned
        runtime_log.write(f"Search requested. job={job.id}; keywords={cleaned}; max={request.max_papers}", "api")
        thread = threading.Thread(target=run_search_job, args=(job, request), daemon=True)
        thread.start()
        return {"job_id": job.id}

    @app.get("/api/projects")
    def projects() -> Response:
        items = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_dir():
                continue
            if path.name.casefold() in reserved_project_dirs:
                continue
            project_id = project_id_from_name(path.name)
            items.append(
                {
                    "id": project_id,
                    "name": project_label(project_id),
                    "active": project_id == active_project_id,
                }
            )
        return json_response({"active": active_project_id or "", "active_name": project_label(active_project_id), "projects": items})

    @app.post("/api/projects")
    def create_project(request: ProjectRequest) -> dict[str, Any]:
        project_id = project_id_from_name(request.name)
        validate_project_id(project_id)
        ensure_project(project_id)
        runtime_log.write(f"Project created. project={project_id}", "project")
        return {"id": project_id, "name": project_label(project_id)}

    @app.post("/api/projects/select")
    def select_project(request: ProjectRequest) -> dict[str, Any]:
        nonlocal active_project_id
        project_id = project_id_from_name(request.name)
        validate_project_id(project_id)
        if not (root / project_id).is_dir():
            raise HTTPException(404, "Project not found.")
        ensure_project(project_id)
        active_project_id = project_id
        save_active_project()
        search_results.clear()
        jobs.clear()
        runtime_log.write(f"Project selected. project={project_id}", "project")
        return {"id": project_id, "name": project_label(project_id)}

    @app.get("/api/repositories")
    def repositories() -> Response:
        data = load_repositories()
        return json_response({
            repo_id: public_papers(papers)
            for repo_id, papers in data.items()
        })

    @app.get("/api/repositories/summary")
    def repositories_summary() -> dict[str, list[dict[str, str]]]:
        data = load_repositories()
        return {
            repo_id: [
                {"key": candidate_key(paper)}
                for paper in papers
            ]
            for repo_id, papers in data.items()
        }

    @app.get("/api/repositories/{repo_id}")
    def repository(repo_id: str) -> Response:
        repo_id = normalize_repo_id(repo_id)
        return json_response(public_papers(load_repositories()[repo_id]))

    @app.post("/api/repositories/{repo_id}/add")
    def add_to_repository(repo_id: str, request: AddRepositoryRequest) -> dict[str, int]:
        repo_id = normalize_repo_id(repo_id)
        runtime_log.write(
            f"Add to repository requested. repo={repo_id}; search_job={request.search_job_id}; "
            f"items={len(request.paper_ids)}",
            "api",
        )
        papers = search_results.get(request.search_job_id)
        if not papers:
            raise HTTPException(404, "Search result is not ready.")

        selected = []
        selected_seen = set()
        for paper_id in request.paper_ids:
            key = paper_id.lower()
            if key and key not in selected_seen:
                selected.append(key)
                selected_seen.add(key)
        if not selected:
            raise HTTPException(400, "No papers selected.")

        repositories = load_repositories()
        repo = repositories[repo_id]
        existing = {candidate_key(item) for item in repo}
        by_key = {candidate_key(item): item for item in papers}
        added = 0
        skipped = 0

        for paper_id in selected:
            paper = by_key.get(paper_id)
            if not paper:
                skipped += 1
                continue
            key = candidate_key(paper)
            if key in existing:
                skipped += 1
                continue
            repo.append(dict(paper))
            existing.add(key)
            added += 1

        save_repositories(repositories)
        return {"added": added, "skipped": skipped, "total": len(repo)}

    @app.delete("/api/repositories/{repo_id}/items/{index}")
    def delete_repository_item(repo_id: str, index: int) -> dict[str, int]:
        repo_id = normalize_repo_id(repo_id)
        runtime_log.write(f"Delete repository item requested. repo={repo_id}; index={index}", "api")
        repositories = load_repositories()
        repo = repositories[repo_id]
        if index < 0 or index >= len(repo):
            raise HTTPException(404, "Repository item not found.")
        del repo[index]
        save_repositories(repositories)
        return {"total": len(repo)}

    @app.post("/api/repositories/{repo_id}/download")
    def download_repository(repo_id: str) -> dict[str, str]:
        repo_id = normalize_repo_id(repo_id)
        papers = load_repositories()[repo_id]
        if not papers:
            raise HTTPException(400, "Repository is empty.")
        job = JobState(str(uuid.uuid4()), "download")
        jobs[job.id] = job
        runtime_log.write(f"Repository download requested. repo={repo_id}; job={job.id}; total={len(papers)}", "api")
        thread = threading.Thread(target=run_download_job, args=(job, papers, repo_id), daemon=True)
        thread.start()
        return {"job_id": job.id}

    @app.post("/api/repositories/{repo_id}/download-one")
    def download_one_to_repository(repo_id: str, request: DownloadOneRequest) -> dict[str, str]:
        repo_id = normalize_repo_id(repo_id)
        paper = find_known_paper(request.paper_id, request.search_job_id)
        if not paper:
            raise HTTPException(404, "Paper not found in search or repositories.")
        repo = ensure_paper_in_repository(repo_id, paper)
        key = candidate_key(paper)
        target_paper = next((item for item in repo if candidate_key(item) == key), paper)
        job = JobState(str(uuid.uuid4()), "download")
        jobs[job.id] = job
        thread = threading.Thread(target=run_download_job, args=(job, [target_paper], repo_id), daemon=True)
        thread.start()
        return {"job_id": job.id}

    @app.post("/api/repositories/{repo_id}/manual-pdf")
    def manual_pdf_to_repository(repo_id: str, request: DownloadOneRequest) -> dict[str, str]:
        repo_id = normalize_repo_id(repo_id)
        runtime_log.write(
            f"Manual source PDF requested. repo={repo_id}; paper={request.paper_id}; "
            f"search_job={request.search_job_id}",
            "api",
        )
        paper = find_known_paper(request.paper_id, request.search_job_id)
        if not paper:
            raise HTTPException(404, "Paper not found in search or repositories.")
        repo = ensure_paper_in_repository(repo_id, paper)
        key = candidate_key(paper)
        target_paper = next((item for item in repo if candidate_key(item) == key), paper)
        job = JobState(str(uuid.uuid4()), "manual_pdf")
        jobs[job.id] = job
        thread = threading.Thread(target=run_manual_pdf_job, args=(job, target_paper, repo_id), daemon=True)
        thread.start()
        return {"job_id": job.id}

    @app.post("/api/open-article-home")
    async def open_article_home(request: Request) -> dict[str, str]:
        data = await request.json()
        value = str(data.get("url") or "").strip()
        if not re.match(r"^https?://", value, re.IGNORECASE):
            raise HTTPException(400, "Article URL must be http(s).")
        os.startfile(value)
        return {"status": "opened"}

    @app.post("/api/repositories/{repo_id}/import-pdf")
    def import_pdf_to_repository(repo_id: str, request: ImportPdfRequest) -> dict[str, str]:
        repo_id = normalize_repo_id(repo_id)
        source = Path(request.pdf_path)
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise HTTPException(400, "Please select a valid PDF file.")
        paper = find_known_paper(request.paper_id, request.search_job_id)
        if not paper:
            raise HTTPException(404, "Paper not found in search or repositories.")
        repo = ensure_paper_in_repository(repo_id, paper)
        key = candidate_key(paper)
        target_paper = next((item for item in repo if candidate_key(item) == key), paper)
        target_dir = downloads_dir() / repo_id
        target_dir.mkdir(parents=True, exist_ok=True)
        title = safe_filename(target_paper.get("title") or source.stem, "paper")
        desired_target = target_dir / f"{title}.pdf"
        if source.resolve() == desired_target.resolve():
            target = desired_target
        else:
            target = unique_pdf_path(target_dir, title)
            shutil.move(str(source), str(target))
        updated = dict(target_paper)
        updated["pdf_path"] = str(target)
        project_db().upsert_paper(updated)

        repositories = load_repositories()
        repo = repositories[repo_id]
        for index, item in enumerate(repo):
            if candidate_key(item) == key:
                repo[index] = updated
                break
        else:
            repo.append(updated)
        save_repositories(repositories)
        runtime_log.write(f"Imported local PDF by moving file. repo={repo_id}; paper={request.paper_id}; path={target}", "api")
        return {"pdf_path": str(target)}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return job.snapshot()

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str) -> dict[str, str]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        job.pause_requested = True
        job.log("Pause requested.")
        return {"status": "pausing"}

    @app.get("/api/search-results/{job_id}")
    def get_search_results(job_id: str) -> Response:
        runtime_log.write(f"Search results requested. job={job_id}; count={len(search_results.get(job_id, []))}", "api")
        return json_response(public_papers(search_results.get(job_id, [])))

    @app.get("/api/papers")
    def papers() -> Response:
        return json_response(project_db().list_papers())

    @app.post("/api/quartiles/reload")
    def reload_quartiles() -> dict[str, Any]:
        quartiles.load()
        for paperset in search_results.values():
            for paper in paperset:
                paper["sci_quartile"] = quartiles.lookup(
                    paper.get("journal"),
                    paper.get("issn_l"),
                    paper.get("issns"),
                )
        return {
            "issn_count": len(quartiles.by_issn),
            "title_count": len(quartiles.by_title),
            "path": str(quartile_path),
        }

    def settings_payload() -> dict[str, Any]:
        active_root = project_root(active_project_id) if active_project_id else None
        app_settings = load_app_settings()
        return {
            "download_dir": str(downloads_dir()) if active_project_id else "",
            "quartile_csv": str(quartile_path),
            "quartile_issn_count": len(quartiles.by_issn),
            "quartile_title_count": len(quartiles.by_title),
            "has_project": bool(active_project_id),
            "project": active_project_id or "",
            "project_label": project_label(active_project_id),
            "project_path": str(active_root) if active_root else "",
            "handoff_first_action_seconds": app_settings["handoff_first_action_seconds"],
        }

    @app.get("/api/settings")
    def settings() -> Response:
        payload = settings_payload()
        return json_response(payload)

    @app.post("/api/settings")
    def update_settings(request: AppSettingsRequest) -> Response:
        settings = load_app_settings()
        settings["handoff_first_action_seconds"] = normalize_handoff_seconds(
            request.handoff_first_action_seconds
        )
        save_app_settings(settings)
        runtime_log.write(
            f"Settings updated: handoff_first_action_seconds={settings['handoff_first_action_seconds']}",
            "settings",
        )
        return json_response(settings_payload())

    @app.get("/api/pdf/{paper_id:path}")
    def paper_pdf(paper_id: str) -> FileResponse:
        path = find_pdf_path_for_paper_id(paper_id)
        if path:
            return FileResponse(path, media_type="application/pdf", filename=path.name)
        raise HTTPException(404, "PDF not found.")

    @app.get("/api/pdf-preview/{paper_id:path}")
    def paper_pdf_preview(paper_id: str) -> FileResponse:
        runtime_log.write(f"PDF preview endpoint requested. paper_id={paper_id}", "api")
        path = find_pdf_path_for_paper_id(paper_id)
        if path:
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=path.name,
                content_disposition_type="inline",
            )
        raise HTTPException(404, "PDF not found.")

    @app.get("/source-preview")
    def source_preview(
        url: str,
        paper_id: str,
        home_url: str = "",
        search_job_id: str = "",
        repo_id: str = "search",
    ) -> HTMLResponse:
        value = url.strip()
        if not re.match(r"^https?://", value, re.IGNORECASE):
            raise HTTPException(400, "Source URL must be http(s).")
        home_value = home_url.strip() or value
        if not re.match(r"^https?://", home_value, re.IGNORECASE):
            home_value = value
        safe_url = json.dumps(value)
        safe_home_url = json.dumps(home_value)
        safe_paper_id = json.dumps(paper_id)
        safe_search_job_id = json.dumps(search_job_id)
        safe_repo_id = json.dumps(repo_id if repo_id in {"repo1", "repo2", "repo3"} else "")
        page = f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>PDF Source</title>
    <style>
      html, body {{
        height: 100%;
        margin: 0;
        overflow: hidden;
        background: #f6f7f9;
        font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
      }}
      iframe {{
        width: 100%;
        height: 100%;
        border: 0;
        background: white;
      }}
      .bar {{
        position: fixed;
        right: 14px;
        bottom: 14px;
        z-index: 10;
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px;
        border: 1px solid #cbd5df;
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.96);
        box-shadow: 0 8px 28px rgba(15, 23, 42, 0.16);
      }}
      select, button {{
        height: 32px;
        border-radius: 6px;
        border: 1px solid #b8c3cf;
        background: white;
        color: #18202b;
        font: inherit;
      }}
      select {{
        padding: 0 8px;
      }}
      button {{
        border-color: #145e54;
        background: #1f7a6d;
        color: white;
        padding: 0 12px;
        cursor: pointer;
      }}
      button.secondary {{
        border-color: #b8c3cf;
        background: #ffffff;
        color: #18202b;
      }}
      button:disabled {{
        cursor: wait;
        opacity: 0.72;
      }}
      .status {{
        max-width: 260px;
        color: #475467;
        font-size: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
    </style>
  </head>
  <body>
    <iframe id="sourceFrame" referrerpolicy="no-referrer"></iframe>
    <div class="bar">
      <select id="repoSelect">
        <option value="repo1">仓库 1</option>
        <option value="repo2">仓库 2</option>
        <option value="repo3">仓库 3</option>
      </select>
      <button id="downloadBtn" type="button">手动下载</button>
      <button id="homeBtn" class="secondary" type="button">文章首页</button>
      <button id="importBtn" class="secondary" type="button">导入PDF</button>
      <span id="status" class="status"></span>
    </div>
    <script>
      const sourceUrl = {safe_url};
      const homeUrl = {safe_home_url};
      const paperId = {safe_paper_id};
      const searchJobId = {safe_search_job_id};
      const initialRepoId = {safe_repo_id};
      const frame = document.getElementById("sourceFrame");
      const repoSelect = document.getElementById("repoSelect");
      const downloadBtn = document.getElementById("downloadBtn");
      const homeBtn = document.getElementById("homeBtn");
      const importBtn = document.getElementById("importBtn");
      const statusText = document.getElementById("status");
      frame.src = sourceUrl;
      if (initialRepoId) repoSelect.value = initialRepoId;

      function desktopApi() {{
        return window.pywebview?.api || parent?.pywebview?.api || null;
      }}

      async function api(path, options = {{}}) {{
        const response = await fetch(path, {{
          headers: {{ "Content-Type": "application/json" }},
          ...options,
        }});
        if (!response.ok) {{
          const text = await response.text();
          throw new Error(text || response.statusText);
        }}
        return response.json();
      }}

      async function pollJob(jobId) {{
        const job = await api(`/api/jobs/${{jobId}}`);
        statusText.textContent = job.status === "finished"
          ? "下载完成"
          : job.status === "error"
            ? "下载失败"
            : "下载中...";
        if (["finished", "error"].includes(job.status)) {{
          downloadBtn.disabled = false;
          return;
        }}
        setTimeout(() => pollJob(jobId).catch((err) => {{
          statusText.textContent = err.message;
          downloadBtn.disabled = false;
        }}), 1200);
      }}

      downloadBtn.addEventListener("click", async () => {{
        downloadBtn.disabled = true;
        statusText.textContent = "准备下载...";
        try {{
          const data = await api(`/api/repositories/${{repoSelect.value}}/manual-pdf`, {{
            method: "POST",
            body: JSON.stringify({{
              search_job_id: searchJobId,
              paper_id: paperId,
            }}),
          }});
          statusText.textContent = "下载中...";
          pollJob(data.job_id);
        }} catch (err) {{
          statusText.textContent = err.message;
          downloadBtn.disabled = false;
        }}
      }});

      homeBtn.addEventListener("click", async () => {{
        homeBtn.disabled = true;
        statusText.textContent = "正在打开文章首页...";
        try {{
          await api("/api/open-article-home", {{
            method: "POST",
            body: JSON.stringify({{ url: homeUrl }}),
          }});
          statusText.textContent = "已打开文章首页";
        }} catch (err) {{
          statusText.textContent = err.message;
        }} finally {{
          homeBtn.disabled = false;
        }}
      }});

      importBtn.addEventListener("click", async () => {{
        const pyApi = desktopApi();
        if (!pyApi?.select_pdf_file) {{
          statusText.textContent = "当前窗口不能打开文件选择框";
          return;
        }}
        importBtn.disabled = true;
        statusText.textContent = "请选择PDF文件...";
        try {{
          const selected = await pyApi.select_pdf_file();
          if (!selected?.path) {{
            statusText.textContent = "未选择PDF";
            return;
          }}
          await api(`/api/repositories/${{repoSelect.value}}/import-pdf`, {{
            method: "POST",
            body: JSON.stringify({{
              search_job_id: searchJobId,
              paper_id: paperId,
              pdf_path: selected.path,
            }}),
          }});
          statusText.textContent = "PDF已导入";
        }} catch (err) {{
          statusText.textContent = err.message;
        }} finally {{
          importBtn.disabled = false;
        }}
      }});
    </script>
  </body>
</html>"""
        return HTMLResponse(page)

    @app.get("/preview-tabs")
    def preview_tabs(tabs: str = "", active: int = 0) -> HTMLResponse:
        try:
            decoded = base64.urlsafe_b64decode(tabs.encode("ascii") + b"=" * (-len(tabs) % 4))
            raw_tabs = json.loads(decoded.decode("utf-8"))
        except Exception:
            raw_tabs = []
        safe_tabs = []
        for item in raw_tabs[:12]:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "PDF")[:80]
            if url.startswith("/") or re.match(r"^https?://", url, re.IGNORECASE):
                safe_tabs.append({"title": title, "url": url})
        active_index = max(0, min(int(active or 0), len(safe_tabs) - 1)) if safe_tabs else 0
        page = f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>PDF Preview</title>
    <style>
      html, body {{
        height: 100%;
        margin: 0;
        overflow: hidden;
        background: #f6f7f9;
        color: #18202b;
        font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
      }}
      body {{
        display: grid;
        grid-template-rows: 40px minmax(0, 1fr);
      }}
      .tabs {{
        display: flex;
        align-items: end;
        gap: 4px;
        padding: 6px 8px 0;
        border-bottom: 1px solid #cbd5df;
        background: #edf1f5;
        overflow-x: auto;
      }}
      .tab {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        max-width: 220px;
        height: 34px;
        padding: 0 12px;
        border: 1px solid #cbd5df;
        border-bottom: 0;
        border-radius: 7px 7px 0 0;
        background: #dde5ed;
        color: #344054;
        font-size: 13px;
        white-space: nowrap;
        overflow: hidden;
        cursor: pointer;
      }}
      .tab-title {{
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .tab-close {{
        width: 18px;
        height: 18px;
        border: 0;
        border-radius: 50%;
        background: transparent;
        color: #667085;
        font-size: 16px;
        line-height: 16px;
        padding: 0;
        cursor: pointer;
      }}
      .tab-close:hover {{
        background: #cbd5df;
        color: #101828;
      }}
      .tab.active {{
        background: #ffffff;
        color: #101828;
      }}
      .pane {{
        min-height: 0;
        background: #ffffff;
      }}
      iframe {{
        width: 100%;
        height: 100%;
        border: 0;
        display: none;
      }}
      iframe.active {{
        display: block;
      }}
    </style>
  </head>
  <body>
    <div id="tabs" class="tabs"></div>
    <div id="panes" class="pane"></div>
    <script>
      const tabs = {json.dumps(safe_tabs, ensure_ascii=False)};
      let active = {active_index};
      const tabHost = document.getElementById("tabs");
      const paneHost = document.getElementById("panes");
      function render() {{
        if (active >= tabs.length) active = Math.max(0, tabs.length - 1);
        tabHost.innerHTML = "";
        paneHost.innerHTML = "";
        tabs.forEach((tab, index) => {{
          const button = document.createElement("button");
          button.className = "tab" + (index === active ? " active" : "");
          button.title = tab.title || "PDF";
          button.onclick = () => {{
            active = index;
            render();
          }};
          const title = document.createElement("span");
          title.className = "tab-title";
          title.textContent = tab.title || "PDF";
          button.appendChild(title);
          const close = document.createElement("span");
          close.className = "tab-close";
          close.textContent = "×";
          close.title = "关闭";
          close.onclick = (event) => {{
            event.stopPropagation();
            tabs.splice(index, 1);
            if (active > index) active -= 1;
            else if (active === index) active = Math.min(index, tabs.length - 1);
            render();
          }};
          button.appendChild(close);
          tabHost.appendChild(button);
          const frame = document.createElement("iframe");
          frame.className = index === active ? "active" : "";
          frame.src = tab.url;
          paneHost.appendChild(frame);
        }});
        if (!tabs.length) {{
          paneHost.innerHTML = "";
        }}
      }}
      render();
    </script>
  </body>
</html>"""
        return HTMLResponse(page)

    @app.post("/api/open-downloads")
    def open_downloads() -> dict[str, str]:
        path = downloads_dir()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))
        return {"path": str(path)}

    @app.post("/api/repositories/{repo_id}/open-downloads")
    def open_repository_downloads(repo_id: str) -> dict[str, str]:
        repo_id = normalize_repo_id(repo_id)
        repo_dir = downloads_dir() / repo_id
        repo_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(repo_dir))
        return {"path": str(repo_dir)}

    @app.get("/assets/MarLous.png")
    def marlous_icon() -> FileResponse:
        return FileResponse(
            root / "MarLous.png",
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    app.mount("/assets", NoCacheStaticFiles(directory=ui_dir), name="assets")

    @app.get("/")
    def index() -> HTMLResponse:
        html_text = (ui_dir / "index.html").read_text(encoding="utf-8")
        repository_summary = {
            repo_id: [{"key": candidate_key(paper)} for paper in papers]
            for repo_id, papers in load_repositories().items()
        } if active_project_id else {"repo1": [], "repo2": [], "repo3": []}
        publishers_payload = [asdict(item) for item in FAMOUS_OA_PUBLISHERS]
        initial_settings = (
            "<script>"
            f"window.__INITIAL_PUBLISHERS__ = {json.dumps(publishers_payload, ensure_ascii=True)};"
            f"window.__INITIAL_SETTINGS__ = {json.dumps(settings_payload(), ensure_ascii=True)};"
            f"window.__INITIAL_REPOSITORY_SUMMARY__ = {json.dumps(repository_summary, ensure_ascii=True)};"
            "</script>"
        )
        html_text = html_text.replace('<script src="/assets/app.js', initial_settings + '\n    <script src="/assets/app.js')
        return HTMLResponse(html_text, headers={"Cache-Control": "no-store, max-age=0"})

    return app

