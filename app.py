from __future__ import annotations

import atexit
import os
import socket
import sys
import subprocess
import threading
import time
import traceback
import webbrowser
import base64
import json
from urllib.parse import urlencode, urljoin
from pathlib import Path

import requests
import uvicorn

from paper_oa_downloader import runtime_log
from paper_oa_downloader.server import create_app


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "app.log"
LOCK_HANDLE = None
LOCK_SOCKET = None
LOCK_MUTEX = None
PREVIEW_WINDOW_GAP = 4
PREVIEW_FOLLOW_INTERVAL_SECONDS = 0.75
PREVIEW_IDLE_INTERVAL_SECONDS = 0.3
MAIN_WINDOW_WIDTH = 1280
MAIN_WINDOW_HEIGHT = 820
PREVIEW_WINDOW_WIDTH = int(MAIN_WINDOW_WIDTH * 0.42)
PREVIEW_WINDOW_HEIGHT = MAIN_WINDOW_HEIGHT


def configure_windows_app_identity() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        app_id = "MarLous.DownloadPaperPdf.WebView"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        runtime_log.write(f"Windows AppUserModelID set: {app_id}", "desktop")
    except Exception as exc:
        runtime_log.write(f"Windows AppUserModelID setup skipped: {exc!r}", "desktop")


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    runtime_log.write(message, "desktop")


def acquire_single_instance_lock() -> None:
    global LOCK_HANDLE, LOCK_SOCKET, LOCK_MUTEX
    import msvcrt

    if sys.platform.startswith("win"):
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.GetLastError.restype = ctypes.c_uint
            mutex = kernel32.CreateMutexW(None, True, "Global\\MarLous.DownloadPaperPdf.SingleInstance")
            already_exists = kernel32.GetLastError() == 183
            if already_exists:
                log("Another app.py instance already owns the Windows mutex; exiting duplicate process.")
                sys.exit(0)
            LOCK_MUTEX = mutex
        except Exception as exc:
            log(f"Windows mutex setup skipped: {exc!r}")

    LOCK_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        LOCK_SOCKET.bind(("127.0.0.1", 49281))
        LOCK_SOCKET.listen(1)
    except OSError:
        log("Another app.py instance is already running on the lock port; exiting duplicate process.")
        sys.exit(0)

    lock_path = ROOT / "data" / "app.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    LOCK_HANDLE = lock_path.open("a+b")
    try:
        msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("Another app.py instance is already running; exiting duplicate process.")
        sys.exit(0)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_server(port: int) -> None:
    try:
        runtime_log.write(f"Backend process starting on port {port}.", "backend")
        config = uvicorn.Config(
            create_app(ROOT),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        uvicorn.Server(config).run()
    except Exception:
        log("Server crashed:\n" + traceback.format_exc())
        raise


def run_backend_process(port: int) -> None:
    run_server(port)


def wait_for_server(url: str, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"{url}/api/settings", timeout=1)
            if response.status_code == 200:
                runtime_log.write(f"Backend health check passed: {url}", "desktop")
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"Backend did not start: {last_error}")


def terminate_process_tree(process: subprocess.Popen | None, timeout_seconds: float = 3) -> None:
    if process is None or process.poll() is not None:
        return

    pid = process.pid
    log(f"Stopping backend process tree: PID {pid}")
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            log("taskkill failed, falling back to Popen.kill: " + repr(exc))
            try:
                process.kill()
            except Exception:
                pass
        return

    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()


class DesktopApi:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.main_window = None
        self.preview_window = None
        self.preview_tabs: list[dict[str, str]] = []
        self.active_preview_index = 0
        self.lock = threading.Lock()
        self.following_started = False
        self.preview_owner_key: tuple[int, int] | None = None
        self.preview_created_at = 0.0

    def set_main_window(self, window) -> None:
        with self.lock:
            self.main_window = window
            self.preview_owner_key = None

    def start_following(self) -> None:
        with self.lock:
            if self.following_started:
                return
            self.following_started = True
        threading.Thread(target=self._follow_main_window, daemon=True).start()
        runtime_log.write("Preview follow thread started.", "preview")

    @staticmethod
    def _window_value(value, fallback: int) -> int:
        return fallback if value is None else int(value)

    def _preview_geometry(self) -> tuple[int, int, int, int]:
        main_width = self._window_value(getattr(self.main_window, "width", None), MAIN_WINDOW_WIDTH)
        main_x = self._window_value(getattr(self.main_window, "x", None), 0)
        main_y = self._window_value(getattr(self.main_window, "y", None), 0)
        width = PREVIEW_WINDOW_WIDTH
        height = PREVIEW_WINDOW_HEIGHT
        x = main_x + main_width + PREVIEW_WINDOW_GAP
        y = main_y
        return x, y, width, height

    def _sync_preview_to_main(self) -> None:
        with self.lock:
            preview = self.preview_window
            main = self.main_window
        if not preview or not main:
            return
        x, y, width, height = self._preview_geometry()
        # Treat the preview as a docked companion pane: fixed gap, fixed top edge.
        if getattr(preview, "x", None) != x or getattr(preview, "y", None) != y:
            preview.move(x, y)
        if getattr(preview, "width", None) != width or getattr(preview, "height", None) != height:
            preview.resize(width, height)

    @staticmethod
    def _native_hwnd(window) -> int:
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is None:
            return 0
        try:
            return int(handle.ToInt64())
        except Exception:
            try:
                return int(handle.ToInt32())
            except Exception:
                return 0

    def _bind_preview_z_order_to_main(self) -> None:
        return
        if not sys.platform.startswith("win"):
            return
        with self.lock:
            preview = self.preview_window
            main = self.main_window
        if not preview or not main:
            return
        preview_hwnd = self._native_hwnd(preview)
        main_hwnd = self._native_hwnd(main)
        if not preview_hwnd or not main_hwnd:
            return
        owner_key = (preview_hwnd, main_hwnd)
        with self.lock:
            if self.preview_owner_key == owner_key:
                return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            gwlp_hwndparent = -8
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_noactivate = 0x0010
            swp_noownerzorder = 0x0200
            hwnd_top = 0
            set_window_long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            set_window_long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            set_window_long_ptr.restype = ctypes.c_void_p
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.c_bool
            set_window_long_ptr(preview_hwnd, gwlp_hwndparent, main_hwnd)
            user32.SetWindowPos(
                preview_hwnd,
                hwnd_top,
                0,
                0,
                0,
                0,
                swp_nomove | swp_nosize | swp_noactivate | swp_noownerzorder,
            )
            with self.lock:
                self.preview_owner_key = owner_key
            runtime_log.write("Preview window bound to main window z-order.", "preview")
        except Exception as exc:
            runtime_log.write(f"Preview z-order binding skipped: {exc!r}", "preview")

    def _preview_has_focus(self) -> bool:
        if not sys.platform.startswith("win"):
            return True
        with self.lock:
            preview = self.preview_window
        if not preview:
            return False
        if time.time() - self.preview_created_at < 0.6:
            return True
        preview_hwnd = self._native_hwnd(preview)
        if not preview_hwnd:
            return True
        try:
            import ctypes

            user32 = ctypes.windll.user32
            get_foreground_window = user32.GetForegroundWindow
            get_foreground_window.restype = ctypes.c_void_p
            is_child = user32.IsChild
            is_child.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            is_child.restype = ctypes.c_bool
            get_ancestor = user32.GetAncestor
            get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            get_ancestor.restype = ctypes.c_void_p
            foreground = int(get_foreground_window() or 0)
            if not foreground:
                return True
            return foreground == preview_hwnd or bool(is_child(preview_hwnd, foreground))
        except Exception as exc:
            runtime_log.write(f"Preview focus check skipped: {exc!r}", "preview")
            return True

    def _follow_main_window(self) -> None:
        while True:
            try:
                with self.lock:
                    has_preview = self.preview_window is not None
                if has_preview:
                    if not self._preview_has_focus():
                        self.close_preview()
                        continue
                    self._sync_preview_to_main()
                    self._bind_preview_z_order_to_main()
            except Exception:
                with self.lock:
                    self.preview_window = None
            time.sleep(PREVIEW_FOLLOW_INTERVAL_SECONDS if has_preview else PREVIEW_IDLE_INTERVAL_SECONDS)

    def _preview_tabs_url(self) -> str:
        visible_tabs = self.preview_tabs[-12:]
        payload = json.dumps(visible_tabs, ensure_ascii=False).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        active = max(0, min(self.active_preview_index, len(visible_tabs) - 1)) if visible_tabs else 0
        return urljoin(self.base_url + "/", f"/preview-tabs?tabs={encoded}&active={active}")

    def _destroy_preview_window_only(self) -> None:
        with self.lock:
            preview = self.preview_window
            self.preview_window = None
            self.preview_owner_key = None
            self.preview_created_at = 0.0
        if preview:
            try:
                preview.destroy()
                runtime_log.write("Old preview window destroyed before opening a new one.", "preview")
            except Exception as exc:
                runtime_log.write(f"Old preview window destroy skipped: {exc!r}", "preview")

    def _show_preview_tabs(self) -> dict[str, str]:
        import webview

        tabs_url = self._preview_tabs_url()
        self._destroy_preview_window_only()
        try:
            x, y, width, height = self._preview_geometry()
            preview = webview.create_window(
                "PDF Preview",
                tabs_url,
                width=width,
                height=height,
                x=x,
                y=y,
                min_size=(320, 480),
                resizable=False,
                confirm_close=False,
                focus=True,
            )
            with self.lock:
                self.preview_window = preview
                self.preview_owner_key = None
                self.preview_created_at = time.time()
            self._sync_preview_to_main()
            self._bind_preview_z_order_to_main()
            runtime_log.write("Preview window created with tabs.", "preview")
            return {"status": "created", "url": tabs_url}
        except Exception as exc:
            log("Preview window failed: " + repr(exc))
            webbrowser.open(tabs_url)
            return {"status": "browser", "url": tabs_url}

    def _add_preview_tab(self, title: str, url: str) -> dict[str, str]:
        with self.lock:
            self.preview_tabs = [{"title": title or "PDF", "url": url}]
            self.active_preview_index = 0
        return self._show_preview_tabs()

    def preview_pdf(self, path: str, title: str = "") -> dict[str, str]:
        pdf_url = urljoin(self.base_url + "/", path.lstrip("/"))
        runtime_log.write(f"Preview requested: {pdf_url}", "preview")
        return self._add_preview_tab(title or "PDF", pdf_url)

    def source_pdf(
        self,
        source_url: str,
        paper_id: str,
        search_job_id: str = "",
        repo_id: str = "search",
        title: str = "",
        home_url: str = "",
    ) -> dict[str, str]:
        params = urlencode(
            {
                "url": source_url,
                "home_url": home_url or source_url,
                "paper_id": paper_id,
                "search_job_id": search_job_id,
                "repo_id": repo_id,
            }
        )
        source_preview_url = urljoin(self.base_url + "/", f"/source-preview?{params}")
        runtime_log.write(f"Source preview requested: {source_preview_url}", "preview")
        return self._add_preview_tab(title or "PDF", source_preview_url)

    def select_pdf_file(self) -> dict[str, str]:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="选择PDF文件",
                filetypes=[("PDF files", "*.pdf")],
            )
            root.destroy()
            return {"path": path or ""}
        except Exception as exc:
            runtime_log.write(f"PDF file selection failed: {exc!r}", "preview")
            return {"path": "", "error": str(exc)}

    def select_project_folder(self) -> dict[str, str]:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(
                title="选择项目文件夹",
                initialdir=str(ROOT),
                mustexist=True,
            )
            root.destroy()
            return {"path": path or ""}
        except Exception as exc:
            runtime_log.write(f"Project folder selection failed: {exc!r}", "project")
            return {"path": "", "error": str(exc)}

    def close_preview(self) -> dict[str, str]:
        with self.lock:
            preview = self.preview_window
            self.preview_window = None
            self.preview_tabs = []
            self.active_preview_index = 0
            self.preview_owner_key = None
            self.preview_created_at = 0.0
        if preview:
            try:
                preview.destroy()
                runtime_log.write("Preview window closed.", "preview")
            except Exception:
                pass
        return {"status": "closed"}


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    run_log_path = ROOT / "run.log"
    if os.environ.get("PAPER_OA_LOG_ALREADY_RESET") == "1":
        os.environ["PAPER_OA_RUN_LOG"] = str(run_log_path)
        runtime_log.write("New desktop run started. run.log was already reset by launcher.", "desktop")
    else:
        runtime_log.reset(run_log_path)
        runtime_log.write("New desktop run started. Previous run.log was cleared.", "desktop")
    configure_windows_app_identity()
    acquire_single_instance_lock()
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    log(f"Starting backend at {url}")

    backend_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not backend_python.exists():
        backend_python = Path(sys.executable)
    backend = subprocess.Popen(
        [str(backend_python), str(Path(__file__).resolve()), "--backend", str(port)],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    runtime_log.write(f"Backend process launched: PID {backend.pid}", "desktop")
    cleanup_lock = threading.Lock()
    cleaned_up = False

    def cleanup_backend() -> None:
        nonlocal cleaned_up
        with cleanup_lock:
            if cleaned_up:
                return
            cleaned_up = True
        terminate_process_tree(backend)

    atexit.register(cleanup_backend)
    wait_for_server(url)
    log("Backend is ready")

    if os.environ.get("PAPER_OA_DESKTOP_MODE", "").casefold() == "browser":
        runtime_log.write("Browser mode enabled; opening system browser.", "desktop")
        webbrowser.open(url)
        try:
            while backend.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            return
        finally:
            runtime_log.write("Browser mode ended; cleaning backend.", "desktop")
            cleanup_backend()
        return

    try:
        import webview

        runtime_log.write("WebView module imported.", "desktop")
        desktop_api = DesktopApi(url)

        def on_main_closing() -> None:
            runtime_log.write("Main window is closing.", "desktop")
            desktop_api.close_preview()

        def on_main_closed() -> None:
            runtime_log.write("Main window closed; backend cleanup scheduled.", "desktop")
            threading.Thread(target=cleanup_backend, daemon=True).start()

        main_window = webview.create_window(
            "Download-PaperPdf-MarLous",
            url,
            width=MAIN_WINDOW_WIDTH,
            height=MAIN_WINDOW_HEIGHT,
            min_size=(980, 640),
            resizable=False,
            confirm_close=False,
        )
        runtime_log.write("Main WebView window created.", "desktop")
        desktop_api.set_main_window(main_window)
        main_window.events.closing += on_main_closing
        main_window.events.closed += on_main_closed
        runtime_log.write("Starting WebView event loop.", "desktop")

        def on_webview_ready() -> None:
            try:
                main_window.expose(
                    desktop_api.preview_pdf,
                    desktop_api.source_pdf,
                    desktop_api.close_preview,
                    desktop_api.select_pdf_file,
                    desktop_api.select_project_folder,
                )
                runtime_log.write("Desktop bridge exposed after WebView startup.", "desktop")
            except Exception as exc:
                runtime_log.write(f"Desktop bridge expose failed: {exc!r}", "desktop")
            desktop_api.start_following()

        webview.start(
            on_webview_ready,
            gui="edgechromium",
            private_mode=True,
            icon=str(ROOT / "MarLous.ico"),
        )
        runtime_log.write("WebView event loop ended.", "desktop")
    except Exception as exc:
        log("WebView failed, opening browser: " + repr(exc))
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    finally:
        runtime_log.write("Desktop main loop ended; cleaning backend.", "desktop")
        cleanup_backend()


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--backend":
            run_backend_process(int(sys.argv[2]))
        else:
            main()
    except Exception:
        log("App crashed:\n" + traceback.format_exc())
        raise

