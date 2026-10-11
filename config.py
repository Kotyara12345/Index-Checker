"""Константы, исключения, logging, ConfigManager, CheckResult."""

import os
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict


# ================= КОНСТАНТЫ =================
CONFIG_FILE = Path("config.json")

ENV_YANDEX_KEY = "INDEX_CHECKER_YANDEX_KEY"
ENV_YANDEX_FOLDER = "INDEX_CHECKER_YANDEX_FOLDER"
ENV_GOOGLE_KEY = "INDEX_CHECKER_GOOGLE_KEY"
ENV_GOOGLE_CX = "INDEX_CHECKER_GOOGLE_CX"

XLSX_MAX_ROWS = 100_000
XLSX_MAX_COLS = 100
MAX_URL_LENGTH = 2048

AIOHTTP_TOTAL_TIMEOUT_SEC = 30
AIOHTTP_CONNECT_TIMEOUT_SEC = 10
AIOHTTP_SOCK_CONNECT_TIMEOUT_SEC = 10
AIOHTTP_SOCK_READ_TIMEOUT_SEC = 20
AIOHTTP_CLOSE_GRACE_SEC = 0.4

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429

RATE_LIMIT_RETRY_SEC = 30

# Обрезка тела ответа в сообщениях об ошибках.
ERROR_BODY_PREVIEW_CHARS = 150

PACKAGE_LOGGER_NAME = "index_checker"


# ================= ТИПЫ =================
class CheckResult(TypedDict):
    url: str
    yandex: Optional[bool]
    google: Optional[bool]
    errors: List[str]


# ================= ИСКЛЮЧЕНИЯ =================
class StopRequested(Exception):
    """Пользователь запросил остановку во время ожидания/запроса."""


# ================= LOGGING =================
_LOGGING_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """
    Настраивает логгер пакета `index_checker` один раз за процесс.

    Потокобезопасно: под `threading.Lock` проверяем флаг и добавляем
    handler только при первой инициализации. Это исключает дублирование
    handlers при одновременном вызове `get_logger()` из нескольких
    потоков (например, из GUI-потока и фонового file-loader).
    """
    global _LOGGING_CONFIGURED

    # Быстрая проверка без lock для горячего пути.
    if _LOGGING_CONFIGURED:
        return

    with _LOGGING_LOCK:
        # Повторная проверка под lock — классический double-checked locking.
        if _LOGGING_CONFIGURED:
            return

        package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        package_logger.setLevel(level)
        package_logger.propagate = False

        if not package_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
            package_logger.addHandler(handler)

        _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Возвращает дочерний логгер пространства `index_checker`."""
    setup_logging()
    full_name = name if name.startswith(PACKAGE_LOGGER_NAME + ".") \
        or name == PACKAGE_LOGGER_NAME \
        else f"{PACKAGE_LOGGER_NAME}.{name}"
    return logging.getLogger(full_name)


logger = get_logger(__name__)


# ================= CONFIG MANAGER =================
class ConfigManager:
    """Чтение/запись config.json с логированием + доступ к ключам через env."""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path: Path = path

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            logger.info("config.json не найден — используются пустые настройки")
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            logger.error("Не удалось прочитать %s: %s", self.path, error)
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            logger.error("config.json содержит некорректный JSON: %s", error)
            return {}
        if not isinstance(data, dict):
            logger.error(
                "config.json должен содержать объект, а не %s",
                type(data).__name__,
            )
            return {}
        return data

    def save(self, cfg: Dict[str, Any]) -> None:
        try:
            self.path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as error:
            logger.error("Не удалось записать %s: %s", self.path, error)
            raise
        if os.name == "posix":
            try:
                os.chmod(self.path, 0o600)
            except OSError as error:
                logger.warning(
                    "Не удалось выставить права 600 на %s: %s", self.path, error
                )

    @staticmethod
    def get_credential(cfg: Dict[str, Any], cfg_key: str, env_var: str) -> str:
        return os.getenv(env_var, "").strip() or str(cfg.get(cfg_key, "")).strip()


__all__ = [
    "CONFIG_FILE",
    "ENV_YANDEX_KEY",
    "ENV_YANDEX_FOLDER",
    "ENV_GOOGLE_KEY",
    "ENV_GOOGLE_CX",
    "XLSX_MAX_ROWS",
    "XLSX_MAX_COLS",
    "MAX_URL_LENGTH",
    "AIOHTTP_TOTAL_TIMEOUT_SEC",
    "AIOHTTP_CONNECT_TIMEOUT_SEC",
    "AIOHTTP_SOCK_CONNECT_TIMEOUT_SEC",
    "AIOHTTP_SOCK_READ_TIMEOUT_SEC",
    "AIOHTTP_CLOSE_GRACE_SEC",
    "HTTP_OK",
    "HTTP_BAD_REQUEST",
    "HTTP_UNAUTHORIZED",
    "HTTP_FORBIDDEN",
    "HTTP_TOO_MANY_REQUESTS",
    "RATE_LIMIT_RETRY_SEC",
    "ERROR_BODY_PREVIEW_CHARS",
    "PACKAGE_LOGGER_NAME",
    "CheckResult",
    "StopRequested",
    "setup_logging",
    "get_logger",
    "ConfigManager",
]