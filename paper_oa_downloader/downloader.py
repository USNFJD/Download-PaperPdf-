from __future__ import annotations

import re
import time
import html
import os
import subprocess
import sys
import shutil
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from . import runtime_log


def safe_filename(value: str, fallback: str = "paper") -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"</[^>]+>\s*and\s*<[^>]+>", " and ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    value = re.sub(r"[\\/:*?\"<>|]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value[:150].strip()
    return value or fallback


def unique_pdf_path(download_dir: Path, title: str) -> Path:
    target = download_dir / f"{title}.pdf"
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = download_dir / f"{title} ({index}).pdf"
        if not candidate.exists():
            return candidate
    return download_dir / f"{title} {int(time.time())}.pdf"


def reusable_pdf_path(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value)
    except Exception:
        return None
    if path.is_file() and path.suffix.lower() == ".pdf" and path.stat().st_size > 1024:
        return path
    return None


class PdfDownloader:
    def __init__(self, download_dir: Path, handoff_first_action_seconds: int = 10):
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir = self.download_dir.parent / ".browser_profile"
        self.handoff_first_action_seconds = handoff_first_action_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def download(self, paper: dict[str, Any]) -> Path:
        title = safe_filename(paper.get("title") or "paper")
        runtime_log.write(f"Download requested. title={title}; dir={self.download_dir}", "download")
        doi_suffix = safe_filename((paper.get("doi") or paper.get("id") or "").split("/")[-1], "oa")
        target = self.download_dir / f"{title}.pdf"
        if target.exists() and target.stat().st_size > 1024:
            runtime_log.write(f"Using existing PDF. path={target}", "download")
            return target
        old_target = self.download_dir / f"{title} [{doi_suffix}].pdf"
        if old_target.exists() and old_target.stat().st_size > 1024:
            if not target.exists():
                old_target.rename(target)
                return target
            return old_target
        target = unique_pdf_path(self.download_dir, title)
        existing = reusable_pdf_path(paper.get("pdf_path"))
        if existing:
            runtime_log.write(f"Copying reusable PDF. source={existing}; target={target}", "download")
            shutil.copy2(existing, target)
            return target
        if not paper.get("pdf_url"):
            raise RuntimeError("这篇文献没有可直接下载的 PDF 链接，请点击源尝试手动打开。")

        try:
            runtime_log.write(f"Trying direct PDF download. url={paper.get('pdf_url')}", "download")
            return self._download_with_requests(paper, target)
        except Exception as exc:
            runtime_log.write(f"Direct PDF download failed; trying browser fallback. error={exc}", "download")
            return self._download_with_browser_subprocess(paper, target, exc)

    def download_with_visible_browser(self, paper: dict[str, Any]) -> Path:
        title = safe_filename(paper.get("title") or "paper")
        runtime_log.write(f"Manual source download requested. title={title}; dir={self.download_dir}", "download")
        target = self.download_dir / f"{title}.pdf"
        if target.exists() and target.stat().st_size > 1024:
            runtime_log.write(f"Manual source already downloaded. path={target}", "download")
            return target
        target = unique_pdf_path(self.download_dir, title)
        existing = reusable_pdf_path(paper.get("pdf_path"))
        if existing:
            runtime_log.write(f"Manual source copying reusable PDF. source={existing}; target={target}", "download")
            shutil.copy2(existing, target)
            return target
        if not paper.get("pdf_url"):
            raise RuntimeError("这篇文献没有可直接打开的 PDF 链接，当前只能记录到仓库，不能自动保存 PDF。")
        return self._download_with_browser_subprocess(
            paper,
            target,
            RuntimeError("Visible browser PDF download was requested."),
            manual=True,
        )

    def _download_with_requests(self, paper: dict[str, Any], target: Path) -> Path:
        headers = {}
        if paper.get("source_url"):
            headers["Referer"] = paper["source_url"]

        with self.session.get(paper["pdf_url"], headers=headers, stream=True, allow_redirects=True, timeout=25) as response:
            runtime_log.write(
                f"Direct PDF response. status={response.status_code}; url={response.url}; "
                f"content_type={response.headers.get('content-type', '')}",
                "download",
            )
            if response.status_code in {401, 403}:
                host = urlparse(response.url).netloc or urlparse(paper["pdf_url"]).netloc
                raise RuntimeError(f"{host} 拒绝后台直接下载 PDF，可能需要网页登录、验证码或站点反爬限制。请点击源链接手动打开。")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            parsed_path = urlparse(response.url).path.lower()
            first = response.raw.read(5, decode_content=True)
            looks_html = first.lstrip().startswith((b"<!DOC", b"<html", b"<HTML"))
            looks_pdf = first.startswith(b"%PDF")
            if not looks_pdf:
                if looks_html or "html" in content_type:
                    raise RuntimeError(f"站点返回网页而不是 PDF，可能需要网页登录、验证码或手动点击下载：{response.url}")
                if "pdf" in content_type or parsed_path.endswith(".pdf"):
                    raise RuntimeError(f"PDF 链接返回的内容不是完整 PDF：{response.url}")
                raise RuntimeError(f"链接没有返回 PDF：{response.url}")

            with target.open("wb") as handle:
                handle.write(first)
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
        time.sleep(0.25)
        if target.stat().st_size <= 1024:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"PDF 保存失败：文件过小或不完整：{response.url}")
        runtime_log.write(f"Direct PDF saved. path={target}", "download")
        return target

    def _download_with_browser_subprocess(
        self,
        paper: dict[str, Any],
        target: Path,
        original_error: Exception,
        manual: bool = False,
    ) -> Path:
        script = Path(__file__).with_name("browser_pdf_fetcher.py")
        if not script.exists():
            raise RuntimeError(f"后台直连失败，且浏览器下载脚本不存在：{original_error}") from original_error

        target = target.resolve()
        profile_dir = self.browser_profile_dir.resolve()
        try:
            runtime_log.write(
                f"Launching browser PDF fetcher. manual={manual}; target={target}; url={paper.get('pdf_url') or ''}",
                "download",
            )
            timeout_seconds = 650 if manual else 55
            env = os.environ.copy()
            env["PAPER_OA_HANDOFF_FIRST_ACTION_SECONDS"] = str(self.handoff_first_action_seconds)
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    paper.get("pdf_url") or "",
                    str(target),
                    str(profile_dir),
                    "--manual" if manual else "--auto",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot launch browser PDF fetcher. target={target}; "
                f"profile={profile_dir}; url={paper.get('pdf_url') or ''}; "
                f"{exc}\n{traceback.format_exc()}"
            ) from original_error
        if result.returncode == 0 and target.exists() and target.stat().st_size > 1024:
            runtime_log.write(f"Browser PDF fetcher saved file. target={target}", "download")
            return target
        detail = (result.stderr or result.stdout or "").strip()
        if result.returncode == 7 and not manual:
            runtime_log.write("Browser fetcher detected human verification; retrying with visible manual handoff.", "download")
            return self._download_with_browser_subprocess(paper, target, original_error, manual=True)
        runtime_log.write(f"Browser PDF fetcher failed. returncode={result.returncode}; detail={detail}", "download")
        raise RuntimeError(f"后台直连失败，浏览器兜底也未捕获到 PDF：{detail or original_error}") from original_error

    def _download_with_browser_cookies(self, paper: dict[str, Any], target: Path, original_error: Exception) -> Path:
        try:
            import browser_cookie3
        except Exception as exc:
            raise RuntimeError(f"浏览器 Cookie 组件不可用：{exc}; 原始错误：{original_error}") from original_error

        host = urlparse(paper.get("pdf_url") or "").netloc
        if not host:
            raise RuntimeError(f"没有可读取 Cookie 的域名：{original_error}") from original_error

        cookie_errors = []
        loaded = False
        for loader_name in ("edge", "chrome", "firefox"):
            loader = getattr(browser_cookie3, loader_name, None)
            if not loader:
                continue
            try:
                cookies = loader(domain_name=host)
                if cookies:
                    self.session.cookies.update(cookies)
                    loaded = True
            except Exception as exc:
                cookie_errors.append(f"{loader_name}: {exc}")

        if not loaded:
            detail = "; ".join(cookie_errors) or "没有从浏览器读取到该网站 Cookie"
            raise RuntimeError(f"后台直连失败，并且无法复用浏览器 Cookie：{detail}") from original_error

        return self._download_with_requests(paper, target)

    def _download_with_browser(self, paper: dict[str, Any], target: Path, original_error: Exception) -> Path:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(f"后台直连失败，且浏览器下载组件不可用：{original_error}; {exc}") from original_error

        pdf_url = paper.get("pdf_url")
        if not pdf_url:
            raise RuntimeError(f"后台直连失败，且没有可用 PDF 链接：{original_error}") from original_error

        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = None
            last_error = ""
            for channel in ("msedge", "chrome", "chromium"):
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(self.browser_profile_dir),
                        channel=channel if channel != "chromium" else None,
                        headless=True,
                        accept_downloads=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)
            if context is None:
                raise RuntimeError(f"后台直连失败，且无法启动浏览器兜底下载：{last_error}") from original_error

            try:
                page = context.new_page()
                response = page.goto(pdf_url, wait_until="domcontentloaded", timeout=90000)

                if response and response.ok:
                    content_type = (response.headers.get("content-type") or "").lower()
                    body = response.body()
                    if body.startswith(b"%PDF") or "pdf" in content_type:
                        target.write_bytes(body)
                        return target

                deadline = time.time() + 75
                last_status = response.status if response else 0
                while time.time() < deadline:
                    page.wait_for_timeout(3000)
                    browser_response = context.request.get(pdf_url, timeout=30000)
                    last_status = browser_response.status
                    content_type = (browser_response.headers.get("content-type") or "").lower()
                    body = browser_response.body()
                    if browser_response.ok and (body.startswith(b"%PDF") or "pdf" in content_type):
                        target.write_bytes(body)
                        return target

                raise RuntimeError(
                    f"浏览器已打开该链接，但没有捕获到 PDF 文件，HTTP {last_status}。"
                    "如果页面要求人工验证，请在弹出的浏览器里完成验证后重新点击下载。"
                )
            finally:
                context.close()
