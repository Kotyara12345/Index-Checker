"""Tkinter GUI приложения: ввод URL, настройки, результаты, экспорт."""

import os
import queue
import functools
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Deque
from collections import deque
from datetime import datetime

from .config import (
    ENV_GOOGLE_CX,
    ENV_GOOGLE_KEY,
    ENV_YANDEX_FOLDER,
    ENV_YANDEX_KEY,
    CheckResult,
    ConfigManager,
    StopRequested,
    get_logger,
)
from .excel_exporter import ExcelExporter
from .file_parsers import parse_any_file
from .checker_core import CheckerCore
from .url_utils import (
    dedupe_urls,
    is_valid_url,
    merge_unique_urls,
    normalize_url,
)

try:
    from defusedxml import ElementTree  # noqa: F401
    _HAS_DEFUSEDXML = True
except ImportError:
    _HAS_DEFUSEDXML = False

logger = get_logger(__name__)

WORKER_SHUTDOWN_TIMEOUT_SEC = 2.0
FORCE_CANCEL_TIMEOUT_SEC = 1.0
UI_POLL_INTERVAL_MS = 100

_DEBUG_THREAD_CHECKS = os.getenv("INDEX_CHECKER_DEBUG_THREADS", "").strip() == "1"
_MAIN_THREAD = threading.main_thread()


def _check_main_thread(method_name: str) -> None:
    if threading.current_thread() is _MAIN_THREAD:
        return
    message = f"{method_name} вызван из фонового потока — небезопасно для Tkinter"
    if _DEBUG_THREAD_CHECKS:
        raise RuntimeError(message)
    logger.warning(message)


def on_main_thread(method: Callable) -> Callable:
    """Проверяет main-thread, но пробрасывает TclError наверх."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        _check_main_thread(method.__name__)
        return method(self, *args, **kwargs)
    return wrapper


def gui_safe(method: Callable) -> Callable:
    """
    Декоратор для публичных обработчиков Tkinter.

    Различает два класса исключений:
      • tk.TclError — штатная ситуация при уничтожении виджетов;
        логируется на уровне WARNING, помечается флаг `_widget_errors`.
      • любое другое исключение — неожиданная ошибка разработки;
        логируется через logger.exception (ERROR + traceback),
        не маскируется под TclError и пробрасывается дальше.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        _check_main_thread(method.__name__)
        try:
            return method(self, *args, **kwargs)
        except tk.TclError:
            self._widget_errors = True
            logger.warning(
                "%s: виджеты уничтожены (TclError) — операция прервана",
                method.__name__,
            )
            return None
        except Exception:
            # Неожиданная ошибка — не связана с уничтожением виджетов.
            # Логируем с traceback и пробрасываем: не наша задача — глушить.
            logger.exception("Неожиданная ошибка в %s", method.__name__)
            raise
    return wrapper


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Проверка индексации — Yandex & Google")
        self.geometry("1100x820")
        self.minsize(950, 700)

        self.config_mgr = ConfigManager()
        self.exporter = ExcelExporter()
        self.cfg: Dict[str, Any] = self.config_mgr.load()

        self.results: List[CheckResult] = []
        self._results_lock: threading.Lock = threading.Lock()
        self.ui_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()

        # _state_lock защищает:
        #   • _worker_thread — создание/чтение (атомарно с _is_worker_alive);
        #   • _loop / _worker_task / _async_stop — обмен между GUI и воркером;
        #   • _stop_requested — флаг «остановка запрошена».
        self._state_lock: threading.Lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._async_stop: Optional[asyncio.Event] = None
        self._stop_requested: bool = False

        self._pending_destroy: bool = False
        self._closing: bool = False
        self._dialog_queue: Deque[Tuple[str, str]] = deque()
        self._dialog_active: bool = False
        self._widget_errors: bool = False

        self._build_ui()
        self._load_keys_to_form()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(UI_POLL_INTERVAL_MS, self._poll_queue)

    # ---------- Интерфейс ----------
    def _build_ui(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        tab_check = ttk.Frame(nb)
        nb.add(tab_check, text="Проверка ссылок")
        self._build_check_tab(tab_check)

        tab_settings = ttk.Frame(nb)
        nb.add(tab_settings, text="API ключи")
        self._build_settings_tab(tab_settings)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        pad = {"padx": 10, "pady": 6}

        frame_y = ttk.LabelFrame(parent, text="Yandex Search API v2")
        frame_y.pack(fill="x", **pad)
        ttk.Label(frame_y, text="API-ключ:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.entry_y_key = ttk.Entry(frame_y, width=70, show="*")
        self.entry_y_key.grid(row=0, column=1, sticky="we", padx=8, pady=6)
        ttk.Label(frame_y, text="Folder ID:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.entry_y_folder = ttk.Entry(frame_y, width=70)
        self.entry_y_folder.grid(row=1, column=1, sticky="we", padx=8, pady=6)

        frame_g = ttk.LabelFrame(parent, text="Google Custom Search JSON API")
        frame_g.pack(fill="x", **pad)
        ttk.Label(frame_g, text="API-ключ:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.entry_g_key = ttk.Entry(frame_g, width=70, show="*")
        self.entry_g_key.grid(row=0, column=1, sticky="we", padx=8, pady=6)
        ttk.Label(frame_g, text="CX (ID движка):").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.entry_g_cx = ttk.Entry(frame_g, width=70)
        self.entry_g_cx.grid(row=1, column=1, sticky="we", padx=8, pady=6)

        frame_opts = ttk.LabelFrame(parent, text="Параметры запросов")
        frame_opts.pack(fill="x", **pad)
        ttk.Label(frame_opts, text="Yandex: запросов в секунду").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.entry_y_rps = ttk.Entry(frame_opts, width=10)
        self.entry_y_rps.insert(0, "0.2")
        self.entry_y_rps.grid(row=0, column=1, sticky="w", padx=8, pady=6)
        ttk.Label(frame_opts, text="Google: запросов в секунду").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.entry_g_rps = ttk.Entry(frame_opts, width=10)
        self.entry_g_rps.insert(0, "0.1")
        self.entry_g_rps.grid(row=1, column=1, sticky="w", padx=8, pady=6)

        hint = (
            "RPS применяются сразу при запуске проверки — «Сохранить» не обязательно.\n"
            "Ключи можно задать через env (приоритет над config.json):\n"
            f"  {ENV_YANDEX_KEY}, {ENV_YANDEX_FOLDER}\n"
            f"  {ENV_GOOGLE_KEY}, {ENV_GOOGLE_CX}\n"
            "config.json сохраняется с правами 600 на POSIX."
        )
        ttk.Label(frame_opts, text=hint, foreground="#555", justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 8)
        )

        if not _HAS_DEFUSEDXML:
            ttk.Label(
                parent,
                text="⚠ defusedxml не установлен — XML парсится стандартным ElementTree.\n"
                     "Рекомендуется: pip install defusedxml",
                foreground="#a12a2a", justify="left",
            ).pack(fill="x", padx=10, pady=(0, 6))

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", **pad)
        ttk.Button(btn_frame, text="Сохранить настройки", command=self.save_settings).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Показать/скрыть ключи", command=self.toggle_keys).pack(side="left", padx=5)

    def _build_check_tab(self, parent: ttk.Frame) -> None:
        top = ttk.LabelFrame(parent, text="Ссылки для проверки (по одной в строке)")
        top.pack(fill="both", expand=False, padx=5, pady=5)
        inner = ttk.Frame(top)
        inner.pack(fill="both", expand=True, padx=5, pady=5)
        self.txt_urls = tk.Text(
            inner, height=8, wrap="none", undo=False, autoseparators=False,
        )
        self.txt_urls.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.txt_urls.yview)
        scroll.pack(side="right", fill="y")
        self.txt_urls.configure(yscrollcommand=scroll.set)
        self.txt_urls.bind("<<Paste>>", self._on_paste)

        load_frame = ttk.Frame(parent)
        load_frame.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Label(load_frame, text="Загрузить из файла:").pack(side="left", padx=(5, 8))
        ttk.Button(load_frame, text="📂 TXT / CSV / XLSX", command=self.load_from_file).pack(side="left", padx=3)
        ttk.Button(load_frame, text="📄 TXT", command=lambda: self.load_from_file([("TXT", "*.txt")])).pack(side="left", padx=3)
        ttk.Button(load_frame, text="📊 CSV", command=lambda: self.load_from_file([("CSV", "*.csv")])).pack(side="left", padx=3)
        ttk.Button(load_frame, text="📗 Excel", command=lambda: self.load_from_file([("Excel", "*.xlsx *.xlsm")])).pack(side="left", padx=3)
        ttk.Button(load_frame, text="🔁 Удалить дубликаты", command=self.dedupe_manual).pack(side="right", padx=5)
        ttk.Button(load_frame, text="🗑 Очистить список", command=self.clear_urls_only).pack(side="right", padx=5)

        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=5, pady=5)
        self.chk_yandex = tk.BooleanVar(value=True)
        self.chk_google = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="Yandex", variable=self.chk_yandex).pack(side="left", padx=5)
        ttk.Checkbutton(ctrl, text="Google", variable=self.chk_google).pack(side="left", padx=5)
        self.btn_start = ttk.Button(ctrl, text="▶ Запустить проверку", command=self.start_check)
        self.btn_start.pack(side="left", padx=15)
        self.btn_stop = ttk.Button(ctrl, text="■ Стоп", command=self.stop_check, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        ttk.Button(ctrl, text="Очистить всё", command=self.clear_all).pack(side="left", padx=5)
        ttk.Button(ctrl, text="Экспорт в Excel", command=self.export_excel).pack(side="left", padx=5)

        stats = ttk.LabelFrame(parent, text="Статистика")
        stats.pack(fill="x", padx=5, pady=5)
        self.lbl_stats = ttk.Label(
            stats,
            text="Всего URL: 0   |   Проиндексировано: 0   |   Не проиндексировано: 0   |   Ошибок: 0",
            font=("Segoe UI", 10, "bold"),
        )
        self.lbl_stats.pack(anchor="w", padx=10, pady=8)
        self.progress = ttk.Progressbar(stats, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        table_frame = ttk.LabelFrame(parent, text="Результаты")
        table_frame.pack(fill="both", expand=True, padx=5, pady=5)
        columns = ("url", "yandex", "google", "error")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        for column, title, width, anchor in (
            ("url", "URL", 420, "w"),
            ("yandex", "Yandex", 140, "center"),
            ("google", "Google", 140, "center"),
            ("error", "Ошибка", 280, "w"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor)
        self.tree.tag_configure("ok", foreground="#0a7a0a")
        self.tree.tag_configure("no", foreground="#a12a2a")
        self.tree.tag_configure("err", foreground="#b8860b")
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        tree_scroll.pack(side="right", fill="y")

        log_frame = ttk.LabelFrame(parent, text="Журнал")
        log_frame.pack(fill="both", expand=False, padx=5, pady=5)
        self.txt_log = tk.Text(
            log_frame, height=6, state="disabled", wrap="word", undo=False,
        )
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)

    # ---------- Очередь UI ----------
    @on_main_thread
    def _poll_queue(self) -> None:
        if self._pending_destroy or self._widget_errors:
            self._try_destroy()
            return

        try:
            while True:
                try:
                    msg = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    logger.exception("Не удалось получить сообщение из UI-очереди")
                    break

                try:
                    self._handle_msg(msg)
                except tk.TclError:
                    self._widget_errors = True
                    self._pending_destroy = True
                    break
                except Exception:
                    logger.exception(
                        "Ошибка обработки сообщения UI-очереди: %r", msg[0],
                    )
        finally:
            if not self._pending_destroy and not self._widget_errors:
                try:
                    self.after(UI_POLL_INTERVAL_MS, self._poll_queue)
                except tk.TclError:
                    self._widget_errors = True
                    self._pending_destroy = True

        if self._pending_destroy or self._widget_errors:
            self._try_destroy()
            return

        self._maybe_show_next_dialog()

    def _try_destroy(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
        except Exception:
            logger.exception("Неожиданная ошибка при destroy()")

    @on_main_thread
    def _handle_msg(self, msg: Tuple[str, Any]) -> None:
        kind = msg[0]
        if kind == "log":
            self._append_log(msg[1])
        elif kind == "row":
            self._add_row(msg[1])
        elif kind == "progress":
            self.progress.configure(value=msg[1])
        elif kind == "stats":
            self._update_stats()
        elif kind == "done":
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
        elif kind == "load_result":
            self._handle_load_result(*msg[1])
        elif kind in ("info_dialog", "warning_dialog", "error_dialog"):
            self._dialog_queue.append((kind.split("_")[0], msg[1]))

    @on_main_thread
    def _maybe_show_next_dialog(self) -> None:
        if self._dialog_active or not self._dialog_queue or self._pending_destroy:
            return
        self._dialog_active = True
        kind, text = self._dialog_queue.popleft()
        try:
            self.after(10, self._do_show_dialog, kind, text)
        except tk.TclError:
            self._dialog_active = False
            self._widget_errors = True

    @on_main_thread
    def _do_show_dialog(self, kind: str, text: str) -> None:
        try:
            if kind == "info":
                messagebox.showinfo("Готово", text)
            elif kind == "warning":
                messagebox.showwarning("Внимание", text)
            else:
                messagebox.showerror("Ошибка", text)
        except tk.TclError:
            self._widget_errors = True
        finally:
            self._dialog_active = False
            if not self._pending_destroy and not self._widget_errors:
                try:
                    self.after(10, self._maybe_show_next_dialog)
                except tk.TclError:
                    self._widget_errors = True

    @gui_safe
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True

        try:
            self.withdraw()
        except tk.TclError:
            self._widget_errors = True

        self._request_stop()

        threading.Thread(
            target=self._wait_worker_and_flag,
            daemon=True,
            name="shutdown-waiter",
        ).start()

    def _wait_worker_and_flag(self) -> None:
        worker = self._get_worker_thread()
        if worker is not None and worker.is_alive():
            worker.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SEC)
            if worker.is_alive():
                logger.warning(
                    "Graceful stop didn't work in %.1fs — force-cancelling task",
                    WORKER_SHUTDOWN_TIMEOUT_SEC,
                )
                self._force_cancel_task()
                worker.join(timeout=FORCE_CANCEL_TIMEOUT_SEC)
                if worker.is_alive():
                    logger.warning(
                        "Worker still alive after force-cancel — "
                        "daemon thread will be cleaned up on process exit",
                    )
        self._pending_destroy = True

    # ---------- Управление loop-состоянием и воркером ----------
    def _set_worker_thread(self, thread: Optional[threading.Thread]) -> None:
        with self._state_lock:
            self._worker_thread = thread

    def _get_worker_thread(self) -> Optional[threading.Thread]:
        with self._state_lock:
            return self._worker_thread

    def _try_start_worker(
        self,
        target: Callable[..., None],
        args: Tuple[Any, ...],
        name: str = "index-checker-worker",
    ) -> bool:
        """
        Атомарно проверяет, что воркер не запущен, и стартует его.

        Возвращает:
          • True  — поток успешно создан и запущен;
          • False — воркер уже выполняется.

        Check-then-act под одним `_state_lock` — исключает гонку при
        быстрых кликах «Старт».
        """
        with self._state_lock:
            current = self._worker_thread
            if current is not None and current.is_alive():
                return False
            worker = threading.Thread(
                target=target, args=args, daemon=True, name=name,
            )
            self._worker_thread = worker
        # Запуск вне lock, чтобы не держать блокировку во время старта потока.
        worker.start()
        return True

    def _set_loop_state(
        self,
        loop: Optional[asyncio.AbstractEventLoop],
        task: Optional[asyncio.Task],
        stop_event: Optional[asyncio.Event],
    ) -> None:
        with self._state_lock:
            self._loop = loop
            self._worker_task = task
            self._async_stop = stop_event

    def _get_loop_state(
        self,
    ) -> Tuple[
        Optional[asyncio.AbstractEventLoop],
        Optional[asyncio.Task],
        Optional[asyncio.Event],
    ]:
        with self._state_lock:
            return self._loop, self._worker_task, self._async_stop

    def _is_stop_requested(self) -> bool:
        with self._state_lock:
            return self._stop_requested

    def _reset_stop_requested(self) -> None:
        with self._state_lock:
            self._stop_requested = False

    def _request_stop(self) -> None:
        with self._state_lock:
            self._stop_requested = True

        loop, _task, stop_event = self._get_loop_state()
        if loop is None or loop.is_closed() or stop_event is None:
            return
        try:
            loop.call_soon_threadsafe(lambda ev=stop_event: ev.set())
        except RuntimeError:
            logger.exception("Не удалось взвести stop_event")

    def _force_cancel_task(self) -> None:
        loop, task, _stop_event = self._get_loop_state()
        if loop is None or loop.is_closed():
            return
        if task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                logger.exception("Не удалось отменить asyncio-задачу")

    # ---------- Потокобезопасный доступ к результатам ----------
    def _append_result(self, row: CheckResult) -> None:
        with self._results_lock:
            self.results.append(row)

    def _snapshot_results(self) -> List[CheckResult]:
        with self._results_lock:
            return list(self.results)

    def _results_len(self) -> int:
        with self._results_lock:
            return len(self.results)

    def _clear_results(self) -> None:
        with self._results_lock:
            self.results.clear()

    def _compute_stats_snapshot(self) -> Tuple[int, int, int, int]:
        with self._results_lock:
            total = len(self.results)
            indexed = 0
            not_indexed = 0
            errors = 0
            for row in self.results:
                if row["yandex"] is True or row["google"] is True:
                    indexed += 1
                if row["yandex"] is False or row["google"] is False:
                    not_indexed += 1
                errors += len(row["errors"])
        return total, indexed, not_indexed, errors

    # ---------- Раздельный сброс UI ----------
    @on_main_thread
    def _clear_urls_ui(self) -> None:
        self.txt_urls.delete("1.0", "end")

    @on_main_thread
    def _clear_results_ui(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._clear_results()
        self.progress["value"] = 0
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")
        self._update_stats()

    @on_main_thread
    def _reset_ui_state(self) -> None:
        self._clear_urls_ui()
        self._clear_results_ui()

    # ---------- Дубликаты ----------
    @on_main_thread
    def _on_paste(self, event: Optional[tk.Event] = None) -> str:
        try:
            clipboard_text = self.clipboard_get()
        except tk.TclError:
            return "break"
        except Exception:
            logger.exception("Неожиданная ошибка чтения буфера обмена")
            return "break"
        self._insert_urls_deduped(clipboard_text)
        return "break"

    @on_main_thread
    def _insert_urls_deduped(self, new_text: str, show_dialog: bool = True) -> int:
        current_text = self.txt_urls.get("1.0", "end")
        existing_keys = {
            normalize_url(line)
            for line in current_text.splitlines()
            if line.strip()
        }

        new_lines: List[str] = []
        duplicates = 0
        for raw_line in new_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            key = normalize_url(stripped)
            if key in existing_keys:
                duplicates += 1
                continue
            existing_keys.add(key)
            new_lines.append(stripped)

        if new_lines:
            prefix = "\n" if current_text.strip() else ""
            try:
                self.txt_urls.insert("end", prefix + "\n".join(new_lines))
                self.txt_urls.see("end")
            except tk.TclError:
                self._widget_errors = True

        if show_dialog and duplicates > 0:
            self._dialog_queue.append(("info", f"Удалено дублирующих URL: {duplicates}"))
            self._maybe_show_next_dialog()

        return duplicates

    @gui_safe
    def dedupe_manual(self) -> None:
        lines = [
            line.strip()
            for line in self.txt_urls.get("1.0", "end").splitlines()
            if line.strip()
        ]
        if not lines:
            messagebox.showinfo("Пусто", "Список ссылок пуст")
            return

        unique_lines, duplicates = dedupe_urls(lines)
        if duplicates == 0:
            messagebox.showinfo("Дубликатов нет", "В списке нет дублирующих URL")
            return

        self.txt_urls.delete("1.0", "end")
        self.txt_urls.insert("1.0", "\n".join(unique_lines))
        messagebox.showinfo(
            "Дубликаты удалены",
            f"Удалено: {duplicates}\nОсталось: {len(unique_lines)}",
        )

    # ---------- Загрузка файлов ----------
    @gui_safe
    def load_from_file(
        self, filetypes: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        if filetypes is None:
            filetypes = [
                ("Все поддерживаемые", "*.txt *.csv *.xlsx *.xlsm"),
                ("Текстовые файлы", "*.txt"),
                ("CSV", "*.csv"),
                ("Excel", "*.xlsx *.xlsm"),
                ("Все файлы", "*.*"),
            ]
        paths = filedialog.askopenfilenames(
            title="Выберите файл(ы)", filetypes=filetypes,
        )
        if not paths:
            return
        self.lbl_stats.config(text="⏳ Загрузка файлов...")
        threading.Thread(
            target=self._load_files_bg,
            args=(paths,),
            daemon=True,
            name="file-loader",
        ).start()

    def _load_files_bg(self, paths: Tuple[str, ...]) -> None:
        all_urls: List[str] = []
        errors: List[str] = []
        for path in paths:
            try:
                urls = parse_any_file(path)
                if not urls:
                    errors.append(f"{Path(path).name}: ссылки не найдены")
                else:
                    all_urls.extend(urls)
            except Exception as error:
                logger.exception("Ошибка парсинга %s", path)
                errors.append(f"{Path(path).name}: {type(error).__name__}: {error}")
        self.ui_queue.put(("load_result", (all_urls, errors, len(paths))))

    @on_main_thread
    def _handle_load_result(
        self, all_urls: List[str], errors: List[str], n_files: int,
    ) -> None:
        self._update_stats()
        if not all_urls:
            msg = "Не удалось найти ни одной ссылки."
            if errors:
                msg += "\n\n" + "\n".join(errors)
            self._dialog_queue.append(("warning", msg))
            self._maybe_show_next_dialog()
            return

        current_lines = [
            line.strip()
            for line in self.txt_urls.get("1.0", "end").splitlines()
            if line.strip()
        ]
        merged, duplicates_removed = merge_unique_urls(current_lines, all_urls)
        added = max(len(merged) - len(current_lines), 0)

        if added or duplicates_removed:
            self.txt_urls.delete("1.0", "end")
            self.txt_urls.insert("1.0", "\n".join(merged))

        parts = [
            f"Файлов: {n_files}",
            f"Из файлов получено: {len(all_urls)}",
            f"Добавлено новых: {added}",
        ]
        if duplicates_removed:
            parts.append(f"Удалено дубликатов: {duplicates_removed}")
        if errors:
            parts.append("")
            parts.append("Предупреждения:")
            parts.extend(errors)
        self._dialog_queue.append(("info", "\n".join(parts)))
        self._maybe_show_next_dialog()

    # ---------- Настройки ----------
    @on_main_thread
    def _load_keys_to_form(self) -> None:
        self.entry_y_key.insert(0, self.cfg.get("yandex_api_key", ""))
        self.entry_y_folder.insert(0, self.cfg.get("yandex_folder_id", ""))
        self.entry_g_key.insert(0, self.cfg.get("google_api_key", ""))
        self.entry_g_cx.insert(0, self.cfg.get("google_cx", ""))
        if "yandex_rps" in self.cfg:
            self.entry_y_rps.delete(0, "end")
            self.entry_y_rps.insert(0, str(self.cfg["yandex_rps"]))
        if "google_rps" in self.cfg:
            self.entry_g_rps.delete(0, "end")
            self.entry_g_rps.insert(0, str(self.cfg["google_rps"]))

    def _read_rps_from_ui(self) -> Tuple[float, float]:
        yandex_raw = self.entry_y_rps.get().strip().replace(",", ".")
        google_raw = self.entry_g_rps.get().strip().replace(",", ".")

        try:
            yandex_rps = float(yandex_raw)
        except ValueError:
            raise ValueError(f"Yandex RPS «{yandex_raw}» — не число")
        try:
            google_rps = float(google_raw)
        except ValueError:
            raise ValueError(f"Google RPS «{google_raw}» — не число")

        if yandex_rps <= 0:
            raise ValueError("Yandex RPS должно быть > 0")
        if google_rps <= 0:
            raise ValueError("Google RPS должно быть > 0")

        return yandex_rps, google_rps

    @on_main_thread
    def _read_rps_or_none(self) -> Optional[Tuple[float, float]]:
        try:
            return self._read_rps_from_ui()
        except ValueError as error:
            messagebox.showerror("Ошибка RPS", str(error))
            return None

    @gui_safe
    def save_settings(self) -> None:
        rps = self._read_rps_or_none()
        if rps is None:
            return
        yandex_rps, google_rps = rps

        self.cfg.update({
            "yandex_api_key": self.entry_y_key.get().strip(),
            "yandex_folder_id": self.entry_y_folder.get().strip(),
            "google_api_key": self.entry_g_key.get().strip(),
            "google_cx": self.entry_g_cx.get().strip(),
            "yandex_rps": yandex_rps,
            "google_rps": google_rps,
        })
        try:
            self.config_mgr.save(self.cfg)
        except OSError as error:
            messagebox.showerror(
                "Ошибка", f"Не удалось сохранить config.json:\n{error}",
            )
            return
        messagebox.showinfo(
            "Сохранено",
            "Настройки сохранены в config.json (права 600 на POSIX).",
        )

    @gui_safe
    def toggle_keys(self) -> None:
        show = "" if self.entry_y_key.cget("show") == "*" else "*"
        self.entry_y_key.config(show=show)
        self.entry_g_key.config(show=show)

    # ---------- Общие действия ----------
    def log(self, msg: str) -> None:
        self.ui_queue.put(("log", msg))

    @on_main_thread
    def _append_log(self, msg: str) -> None:
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", f"{msg}\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    @gui_safe
    def clear_urls_only(self) -> None:
        self._clear_urls_ui()

    @gui_safe
    def clear_all(self) -> None:
        self._reset_ui_state()

    @staticmethod
    def _status_text(value: Optional[bool]) -> str:
        if value is True:
            return "Проиндексирован"
        if value is False:
            return "Не проиндексирован"
        return "Ошибка"

    @on_main_thread
    def _update_stats(self) -> None:
        total, indexed, not_indexed, errors = self._compute_stats_snapshot()
        self.lbl_stats.config(
            text=f"Всего URL: {total}   |   Проиндексировано: {indexed}   |   "
                 f"Не проиндексировано: {not_indexed}   |   Ошибок: {errors}"
        )

    # ---------- Excel ----------
    @gui_safe
    def export_excel(self) -> None:
        if self._results_len() == 0:
            messagebox.showwarning("Нет данных", "Сначала выполните проверку")
            return
        default_name = f"index_report_{datetime.now():%Y-%m-%d_%H-%M}.xlsx"
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("Все файлы", "*.*")],
            initialfile=default_name,
        )
        if not path:
            return

        path_obj = Path(path)
        if path_obj.is_dir():
            path_obj = path_obj / default_name

        parent = path_obj.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
                logger.info("Созданы родительские папки: %s", parent)
            except OSError as error:
                messagebox.showerror(
                    "Ошибка",
                    f"Не удалось создать папку:\n{parent}\n\n{error}",
                )
                return

        if path_obj.suffix.lower() not in (".xlsx", ""):
            logger.warning("Расширение файла не .xlsx: %s", path_obj.suffix)

        snapshot = self._snapshot_results()
        self.ui_queue.put(("log", "⏳ Сохранение отчёта..."))
        threading.Thread(
            target=self._export_bg,
            args=(str(path_obj), snapshot),
            daemon=True,
            name="excel-exporter",
        ).start()

    def _export_bg(self, path: str, results: List[CheckResult]) -> None:
        try:
            self.exporter.write(path, results, self._status_text)
            self.ui_queue.put(("log", f"✓ Отчёт сохранён: {path}"))
            self.ui_queue.put(("info_dialog", f"Отчёт сохранён:\n{path}"))
        except PermissionError as error:
            logger.exception("Нет доступа к файлу Excel %s", path)
            self.ui_queue.put((
                "error_dialog",
                f"Нет доступа к файлу:\n{error}\n\n"
                "Закройте файл в Excel и попробуйте снова.",
            ))
        except FileNotFoundError as error:
            logger.exception("Папка для отчёта исчезла: %s", path)
            self.ui_queue.put((
                "error_dialog",
                f"Папка для сохранения не найдена:\n{error}",
            ))
        except OSError as error:
            logger.exception("Ошибка ФС при сохранении Excel %s", path)
            self.ui_queue.put((
                "error_dialog",
                f"Ошибка файловой системы ({type(error).__name__}):\n{error}",
            ))
        except Exception as error:
            logger.exception("Неожиданная ошибка сохранения Excel %s", path)
            self.ui_queue.put((
                "error_dialog",
                f"Не удалось сохранить файл ({type(error).__name__}):\n{error}",
            ))

    # ---------- Основная логика ----------
    @gui_safe
    def start_check(self) -> None:
        urls = self._collect_and_validate_urls()
        if urls is None:
            return

        do_yandex, do_google = self._read_search_engines()
        if not do_yandex and not do_google:
            messagebox.showwarning(
                "Нечего проверять", "Выберите хотя бы один поисковик",
            )
            return

        if not self._validate_keys(do_yandex, do_google):
            return

        rps = self._read_rps_or_none()
        if rps is None:
            return
        yandex_rps, google_rps = rps

        if not self._confirm_google_limit(do_google, len(urls)):
            return

        # Подготовка UI-состояния — до старта воркера, чтобы при отказе
        # _try_start_worker ничего не пришлось откатывать.
        self._clear_results_ui()
        self.progress["maximum"] = len(urls)
        self._reset_stop_requested()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._maybe_show_next_dialog()

        # Атомарная проверка + запуск под _state_lock.
        started = self._try_start_worker(
            target=self._run_async_checks,
            args=(urls, do_yandex, do_google, yandex_rps, google_rps),
            name="index-checker-worker",
        )
        if not started:
            # Гонка: между двумя кликами «Старт» воркер успел создаться.
            # Возвращаем кнопки в согласованное состояние.
            logger.info("Повторный клик «Старт» — воркер уже запущен")
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")

    @on_main_thread
    def _collect_and_validate_urls(self) -> Optional[List[str]]:
        raw_lines = [
            line.strip()
            for line in self.txt_urls.get("1.0", "end").splitlines()
            if line.strip()
        ]
        if not raw_lines:
            messagebox.showwarning("Нет ссылок", "Введите хотя бы одну ссылку")
            return None

        unique_urls, removed_count = dedupe_urls(raw_lines)
        if removed_count > 0:
            self.txt_urls.delete("1.0", "end")
            self.txt_urls.insert("1.0", "\n".join(unique_urls))
            self._dialog_queue.append(
                ("info", f"Перед запуском удалено дубликатов: {removed_count}")
            )

        invalid = [url for url in unique_urls if not is_valid_url(url)]
        if invalid:
            preview = "\n".join(invalid[:5])
            extra = f"\n... и ещё {len(invalid) - 5}" if len(invalid) > 5 else ""
            msg = (
                f"Некорректных строк: {len(invalid)}\n{preview}{extra}"
                "\n\nПродолжить только с валидными?"
            )
            if not messagebox.askyesno("Некорректные URL", msg):
                return None
            unique_urls = [url for url in unique_urls if is_valid_url(url)]
            if not unique_urls:
                return None

        return unique_urls

    def _read_search_engines(self) -> Tuple[bool, bool]:
        return self.chk_yandex.get(), self.chk_google.get()

    @on_main_thread
    def _validate_keys(self, do_yandex: bool, do_google: bool) -> bool:
        if do_yandex:
            yandex_key = ConfigManager.get_credential(
                self.cfg, "yandex_api_key", ENV_YANDEX_KEY,
            )
            yandex_folder = ConfigManager.get_credential(
                self.cfg, "yandex_folder_id", ENV_YANDEX_FOLDER,
            )
            if not yandex_key or not yandex_folder:
                messagebox.showerror(
                    "Нет ключей", "Заполните Yandex API-ключ и Folder ID",
                )
                return False

        if do_google:
            google_key = ConfigManager.get_credential(
                self.cfg, "google_api_key", ENV_GOOGLE_KEY,
            )
            google_cx = ConfigManager.get_credential(
                self.cfg, "google_cx", ENV_GOOGLE_CX,
            )
            if not google_key or not google_cx:
                messagebox.showerror(
                    "Нет ключей", "Заполните Google API-ключ и CX",
                )
                return False

        return True

    @on_main_thread
    def _confirm_google_limit(self, do_google: bool, url_count: int) -> bool:
        if not do_google or url_count <= 100:
            return True
        return messagebox.askyesno(
            "Внимание: лимит Google API",
            f"Google Custom Search JSON API даёт 100 бесплатных запросов/день.\n"
            f"У вас {url_count} URL — это {url_count} запросов к Google.\n\n"
            "⚠ Превышение лимита может привести к платным списаниям\n"
            "с карты, привязанной к вашему Google Cloud проекту.\n\n"
            "Продолжить?",
        )

    @gui_safe
    def stop_check(self) -> None:
        self._request_stop()
        self.log("⏹ Остановка по запросу пользователя...")

    def _run_async_checks(
        self,
        urls: List[str],
        do_yandex: bool,
        do_google: bool,
        yandex_rps: float,
        google_rps: float,
    ) -> None:
        cancelled = False
        try:
            asyncio.run(
                self._async_worker(urls, do_yandex, do_google, yandex_rps, google_rps)
            )
        except (asyncio.CancelledError, StopRequested):
            cancelled = True
            logger.info("Проверка остановлена пользователем")
        except Exception as error:
            logger.exception("Критическая ошибка воркера")
            self.ui_queue.put((
                "log",
                f"Критическая ошибка ({type(error).__name__}): {error}",
            ))
        finally:
            self._set_loop_state(None, None, None)
            self.ui_queue.put(("done", None))
            self.ui_queue.put((
                "log",
                "=== Проверка отменена ===" if cancelled else "=== Проверка завершена ===",
            ))

    async def _async_worker(
        self,
        urls: List[str],
        do_yandex: bool,
        do_google: bool,
        yandex_rps: float,
        google_rps: float,
    ) -> None:
        if self._is_stop_requested():
            self.ui_queue.put(("log", "⏹ Остановка запрошена до старта — выходим"))
            return

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        stop_event = asyncio.Event()
        self._set_loop_state(loop, task, stop_event)

        if stop_event.is_set() or self._is_stop_requested():
            self.ui_queue.put(("log", "⏹ Остановка запрошена до старта — выходим"))
            return

        core = CheckerCore(
            self.cfg,
            yandex_rps=yandex_rps,
            google_rps=google_rps,
            log_cb=self.log,
        )

        async with core:
            for index, url in enumerate(urls, 1):
                if stop_event.is_set():
                    self.ui_queue.put(("log", "⏹ Прервано пользователем"))
                    break
                try:
                    row = await core.check_one(
                        url, do_yandex, do_google, stop_event=stop_event,
                    )
                except StopRequested:
                    self.ui_queue.put(("log", "⏹ Прервано"))
                    break
                except asyncio.CancelledError:
                    self.ui_queue.put(("log", "⏹ Задача отменена"))
                    raise
                self._append_result(row)
                self.ui_queue.put(("row", row))
                self.ui_queue.put(("progress", index))
                self.ui_queue.put(("stats", None))

        await asyncio.sleep(0)

    @on_main_thread
    def _add_row(self, row: CheckResult) -> None:
        yandex_text = self._status_text(row["yandex"])
        google_text = self._status_text(row["google"])
        error_text = "; ".join(row["errors"])

        if row["errors"] and row["yandex"] is None and row["google"] is None:
            tag = "err"
        elif row["yandex"] is True or row["google"] is True:
            tag = "ok"
        elif row["yandex"] is False or row["google"] is False:
            tag = "no"
        else:
            tag = "err"

        self.tree.insert(
            "", "end",
            values=(row["url"], yandex_text, google_text, error_text),
            tags=(tag,),
        )


__all__ = ["App"]