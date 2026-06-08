from __future__ import annotations

import sys
import time
import traceback
import os
import threading
from pathlib import Path
from urllib.parse import urljoin


MAIN_WINDOW_TITLE = "Download-PaperPdf-MarLous"
FALLBACK_MAIN_WINDOW_WIDTH = 1280
FALLBACK_MAIN_WINDOW_HEIGHT = 820
HANDOFF_WINDOW_GAP = 4
HANDOFF_WINDOW_WIDTH = int(FALLBACK_MAIN_WINDOW_WIDTH * 0.42)
HANDOFF_WINDOW_HEIGHT = FALLBACK_MAIN_WINDOW_HEIGHT
DEFAULT_HANDOFF_FIRST_ACTION_SECONDS = 10
HANDOFF_AFTER_ACTION_SECONDS = 600

HUMAN_VERIFICATION_MARKERS = (
    "verify you are human",
    "human verification",
    "captcha",
    "recaptcha",
    "cf-turnstile",
    "cloudflare",
    "just a moment",
    "access denied",
    "unusual traffic",
    "机器人",
    "真人验证",
    "验证码",
)


def log(message: str) -> None:
    path = os.environ.get("PAPER_OA_RUN_LOG")
    if not path:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [browser] {message}\n")
    except Exception:
        pass


def is_complete_pdf(body: bytes) -> bool:
    if not body.startswith(b"%PDF") or len(body) <= 1024:
        return False
    return b"%%EOF" in body[-4096:]


def candidate_pdf_urls(page, fallback_url: str) -> list[str]:
    urls = []
    seen = set()

    def add(value: str) -> None:
        value = str(value or "").strip()
        if not value:
            return
        absolute = urljoin(page.url or fallback_url, value)
        if absolute and absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)

    add(page.url or fallback_url)
    add(fallback_url)
    for frame in page.frames:
        add(getattr(frame, "url", "") or "")
    for selector in ("iframe[src]", "embed[src]", "object[data]", "a[href*='.pdf']", "a[href*='pdf']"):
        try:
            elements = page.locator(selector)
            count = min(elements.count(), 12)
            for index in range(count):
                element = elements.nth(index)
                add(element.get_attribute("src") or element.get_attribute("data") or element.get_attribute("href") or "")
        except Exception:
            pass
    return urls


def handoff_first_action_seconds() -> int:
    raw = os.environ.get("PAPER_OA_HANDOFF_FIRST_ACTION_SECONDS", "")
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_HANDOFF_FIRST_ACTION_SECONDS
    return value if value in {0, 10, 20, 30, 60} else DEFAULT_HANDOFF_FIRST_ACTION_SECONDS


def handoff_window_geometry() -> tuple[int, int, int, int]:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            hwnd = user32.FindWindowW(None, MAIN_WINDOW_TITLE)
            if hwnd:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    main_width = FALLBACK_MAIN_WINDOW_WIDTH
                    screen_width = max(1, int(user32.GetSystemMetrics(0)))
                    width = min(HANDOFF_WINDOW_WIDTH, max(360, screen_width - int(rect.left + main_width + HANDOFF_WINDOW_GAP)))
                    return (
                        int(rect.left + main_width + HANDOFF_WINDOW_GAP),
                        int(rect.top),
                        width,
                        HANDOFF_WINDOW_HEIGHT,
                    )
        except Exception as exc:
            log(f"Main window geometry lookup failed: {exc!r}")
    return (
        FALLBACK_MAIN_WINDOW_WIDTH + HANDOFF_WINDOW_GAP,
        0,
        HANDOFF_WINDOW_WIDTH,
        HANDOFF_WINDOW_HEIGHT,
    )


def window_rect(hwnd) -> tuple[int, int, int, int] | None:
    if not sys.platform.startswith("win") or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)
    except Exception:
        return None
    return None


def find_handoff_window(expected: tuple[int, int, int, int]):
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        matches = []

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = window_rect(hwnd)
            if not rect:
                return True
            x, y, width, height = rect
            ex, ey, ew, eh = expected
            if ex - 80 <= x <= ex + 260 and abs(y - ey) <= 180 and width >= 320 and height >= 240:
                matches.append(hwnd)
                return False
            return True

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
        user32.EnumWindows(enum_proc, 0)
        return matches[0] if matches else None
    except Exception:
        return None


def handoff_has_focus(expected: tuple[int, int, int, int]) -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes

        hwnd = find_handoff_window(expected)
        if not hwnd:
            return True
        foreground = ctypes.windll.user32.GetForegroundWindow()
        return bool(foreground == hwnd)
    except Exception:
        return True


def start_handoff_window_sync(stop_event: threading.Event, initial_geometry: tuple[int, int, int, int]) -> None:
    if not sys.platform.startswith("win"):
        return

    def worker() -> None:
        try:
            import ctypes

            current_geometry = initial_geometry
            hwnd = None
            while not stop_event.is_set():
                next_geometry = handoff_window_geometry()
                hwnd = hwnd or find_handoff_window(current_geometry) or find_handoff_window(next_geometry)
                if hwnd:
                    x, y, width, height = next_geometry
                    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, width, height, 0x0010)
                    current_geometry = next_geometry
                time.sleep(0.75)
        except Exception as exc:
            log(f"Manual handoff window sync stopped: {exc!r}")

    threading.Thread(target=worker, daemon=True).start()


def sync_handoff_window_once(expected: tuple[int, int, int, int]) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        hwnd = find_handoff_window(expected)
        if hwnd:
            x, y, width, height = handoff_window_geometry()
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, width, height, 0x0010)
    except Exception as exc:
        log(f"Manual handoff window one-shot sync failed: {exc!r}")


def install_user_action_tracker(page) -> None:
    try:
        page.evaluate(
            """() => {
                if (window.__paperOaActionTrackerInstalled) return;
                window.__paperOaActionTrackerInstalled = true;
                window.__paperOaUserActed = false;
                const mark = () => { window.__paperOaUserActed = true; };
                window.addEventListener('click', mark, true);
                window.addEventListener('mousedown', mark, true);
                window.addEventListener('keydown', mark, true);
                window.addEventListener('pointerdown', mark, true);
            }"""
        )
    except Exception as exc:
        log(f"User action tracker install failed: {exc!r}")


def user_has_acted(page, expected_geometry: tuple[int, int, int, int] | None = None) -> bool:
    try:
        if bool(page.evaluate("() => Boolean(window.__paperOaUserActed)")):
            return True
    except Exception:
        pass
    if sys.platform.startswith("win"):
        if expected_geometry and not handoff_has_focus(expected_geometry):
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for key_code in (0x01, 0x02, 0x0D, 0x20):
                if user32.GetAsyncKeyState(key_code) & 0x8000:
                    return True
        except Exception:
            pass
    return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in {3, 4}:
        print("Usage: browser_pdf_fetcher.py <pdf_url> <target> <profile_dir> [--auto|--manual]", file=sys.stderr)
        return 2

    pdf_url = argv[0]
    target = Path(argv[1]).resolve()
    profile_dir = Path(argv[2]).resolve()
    manual = len(argv) == 4 and argv[3] == "--manual"
    log(f"Browser fetcher started. manual={manual}; url={pdf_url}; target={target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"Playwright unavailable: {exc}", file=sys.stderr)
        return 3

    with sync_playwright() as playwright:
        context = None
        last_error = ""
        window_args = []
        viewport = None
        if manual:
            x, y, width, height = handoff_window_geometry()
            window_args = [f"--window-position={x},{y}", f"--window-size={width},{height}"]
            viewport = {"width": width, "height": height}
            log(f"Manual handoff window geometry: x={x}; y={y}; width={width}; height={height}")

        for channel in ("msedge", "chrome", "chromium"):
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=channel if channel != "chromium" else None,
                    accept_downloads=True,
                    headless=not manual,
                    viewport=viewport,
                    args=["--disable-blink-features=AutomationControlled", *window_args],
                )
                break
            except Exception as exc:
                last_error = str(exc)

        if context is None:
            log(f"Cannot launch browser. error={last_error}")
            print(f"Cannot launch browser: {last_error}", file=sys.stderr)
            return 4

        try:
            handoff_geometry = handoff_window_geometry() if manual else None
            stop_window_sync = threading.Event()
            if handoff_geometry:
                start_handoff_window_sync(stop_window_sync, handoff_geometry)
            page = context.new_page()
            if handoff_geometry:
                sync_handoff_window_once(handoff_geometry)
            download_holder = {}
            page.on("download", lambda download: download_holder.setdefault("download", download))
            response = page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
            if manual:
                install_user_action_tracker(page)
            last_status = response.status if response else 0
            log(f"Browser page loaded. status={last_status}; url={page.url}")

            if response and response.ok:
                content_type = (response.headers.get("content-type") or "").lower()
                body = response.body()
                if is_complete_pdf(body):
                    target.write_bytes(body)
                    log(f"PDF body saved directly. target={target}")
                    return 0
                if "pdf" in content_type:
                    log(
                        f"PDF-like response was not a complete PDF. "
                        f"bytes={len(body)}; starts={body[:16]!r}; url={page.url}"
                    )
            if is_human_verification_page(page) and not manual:
                log("Human verification detected; asking for manual handoff.")
                print("SKIP_HUMAN_VERIFICATION: source page requires human verification.", file=sys.stderr)
                return 7

            if manual and is_human_verification_page(page):
                log("Human verification detected; waiting for the user to complete it in the visible browser.")
                print(
                    "MANUAL_HUMAN_VERIFICATION: complete the verification in the opened browser; "
                    "the downloader will continue automatically.",
                    file=sys.stderr,
                )
                wait_seconds = handoff_first_action_seconds()
                if wait_seconds <= 0:
                    log("Manual handoff wait is 0 seconds; skipping.")
                    print("SKIP_HUMAN_VERIFICATION: manual action wait is disabled.", file=sys.stderr)
                    return 7
                first_action_deadline = time.time() + wait_seconds
                while time.time() < first_action_deadline:
                    if user_has_acted(page, handoff_geometry):
                        log("Manual handoff received user action; continuing to wait for verification/PDF.")
                        break
                    page.wait_for_timeout(250)
                else:
                    log(f"Manual handoff had no user action within {wait_seconds} seconds; skipping.")
                    print(f"SKIP_HUMAN_VERIFICATION: no manual action within {wait_seconds} seconds.", file=sys.stderr)
                    return 7

            deadline = time.time() + (HANDOFF_AFTER_ACTION_SECONDS if manual else 35)
            while time.time() < deadline:
                if manual and handoff_geometry and not handoff_has_focus(handoff_geometry):
                    log("Manual handoff lost focus; skipping.")
                    print("SKIP_HUMAN_VERIFICATION: manual handoff lost focus.", file=sys.stderr)
                    return 7
                download = download_holder.get("download")
                if download:
                    temp_target = target.with_name(f"{target.stem}.part{target.suffix}")
                    try:
                        download.save_as(str(temp_target))
                        temp_target.replace(target)
                        log(f"Browser download event saved PDF. target={target}")
                    except Exception:
                        log(f"Download save failed. target={target}; error={traceback.format_exc()}")
                        print(
                            f"Download save failed. target={target}; temp={temp_target}\n{traceback.format_exc()}",
                            file=sys.stderr,
                        )
                        return 6
                    return 0

                page.wait_for_timeout(3000)
                if is_human_verification_page(page) and not manual:
                    log("Human verification detected during wait; skipping.")
                    print("SKIP_HUMAN_VERIFICATION: source page requires human verification.", file=sys.stderr)
                    return 7
                for current_url in candidate_pdf_urls(page, pdf_url):
                    try:
                        browser_response = context.request.get(current_url, timeout=90000)
                        last_status = browser_response.status
                        content_type = (browser_response.headers.get("content-type") or "").lower()
                        body = browser_response.body()
                        if browser_response.ok and is_complete_pdf(body):
                            target.write_bytes(body)
                            log(f"PDF saved from browser request. target={target}; url={browser_response.url}")
                            return 0
                        if browser_response.ok and "pdf" in content_type:
                            log(
                                f"Browser request returned PDF-like incomplete body. "
                                f"bytes={len(body)}; starts={body[:16]!r}; url={browser_response.url}"
                            )
                    except Exception as exc:
                        last_error = str(exc)
                        log(f"Browser request PDF fetch failed during wait: url={current_url}; error={last_error}")

                if manual:
                    try:
                        for frame in page.frames:
                            links = frame.locator(
                                "a[href*='.pdf'], a:has-text('PDF'), a:has-text('Download'), "
                                "a:has-text('Full Text'), button:has-text('PDF'), button:has-text('Download')"
                            )
                            count = min(links.count(), 4)
                            for index in range(count):
                                candidate = links.nth(index)
                                if not candidate.is_visible(timeout=500):
                                    continue
                                with page.expect_download(timeout=4000) as download_info:
                                    candidate.click(timeout=1500)
                                download_holder.setdefault("download", download_info.value)
                                break
                            if download_holder.get("download"):
                                break
                    except Exception:
                        pass

            log(f"Browser did not capture PDF. last_status={last_status}")
            print(
                f"Browser opened the link but did not capture a PDF, HTTP {last_status}. "
                "If verification is required, click the PDF download button in the opened browser before the manual window times out.",
                file=sys.stderr,
            )
            return 5
        finally:
            try:
                stop_window_sync.set()
            except Exception:
                pass
            context.close()


def is_human_verification_page(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=1200).casefold()
    except Exception:
        text = ""
    try:
        html = page.content().casefold()
    except Exception:
        html = ""
    haystack = f"{text}\n{html}"
    return any(marker in haystack for marker in HUMAN_VERIFICATION_MARKERS)


if __name__ == "__main__":
    raise SystemExit(main())
