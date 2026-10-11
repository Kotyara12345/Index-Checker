"""Ядро проверки: RateLimiter, interruptible_wait, CheckerCore."""

import json
import time
import base64
import asyncio
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Callable, Dict, Deque, Optional, Tuple

import aiohttp

try:
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException as _DefusedXmlException
    _HAS_DEFUSEDXML = True
except ImportError:  # pragma: no cover
    SafeET = ET  # noqa: N816
    _HAS_DEFUSEDXML = False

    class _DefusedXmlException(Exception):
        pass

from .config import (
    AIOHTTP_CLOSE_GRACE_SEC,
    AIOHTTP_CONNECT_TIMEOUT_SEC,
    AIOHTTP_SOCK_CONNECT_TIMEOUT_SEC,
    AIOHTTP_SOCK_READ_TIMEOUT_SEC,
    AIOHTTP_TOTAL_TIMEOUT_SEC,
    ENV_GOOGLE_CX,
    ENV_GOOGLE_KEY,
    ENV_YANDEX_FOLDER,
    ENV_YANDEX_KEY,
    ERROR_BODY_PREVIEW_CHARS,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    RATE_LIMIT_RETRY_SEC,
    CheckResult,
    ConfigManager,
    StopRequested,
    get_logger,
)
from .url_utils import build_yandex_query, normalize_url

logger = get_logger(__name__)

if not _HAS_DEFUSEDXML:
    logger.warning(
        "defusedxml не установлен — используется стандартный ElementTree. "
        "Рекомендуется установить: pip install defusedxml"
    )

MAX_JSON_TRAVERSAL_NODES = 100_000


def _format_error(service: str, error: BaseException) -> str:
    type_name = type(error).__name__
    text = str(error).strip()
    if not text:
        return f"{service}: {type_name}"
    if len(text) > 180:
        text = text[:177] + "..."
    return f"{service}: {type_name}: {text}"


async def _safe_response_text(resp: aiohttp.ClientResponse) -> str:
    """
    Безопасно читает текст ответа, обрезая до ERROR_BODY_PREVIEW_CHARS.

    Защищает от:
      • UnicodeDecodeError — если сервер вернул binary-тело с text/html
        Content-Type (charset может быть указан неверно);
      • неожиданных исключений при чтении уже закрытого соединения.

    При любой ошибке возвращает placeholder, не выбрасывая наружу.
    """
    try:
        raw = await resp.read()
    except Exception:
        return "<не удалось прочитать тело ответа>"

    try:
        text = raw.decode(resp.charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        # Неизвестный charset или сбой декодирования — декодируем UTF-8
        # с заменой невалидных байтов.
        text = raw.decode("utf-8", errors="replace")

    return text[:ERROR_BODY_PREVIEW_CHARS]


# ================= RATE LIMITER =================
async def interruptible_wait(
    seconds: float, stop_event: Optional[asyncio.Event]
) -> None:
    if seconds <= 0:
        return
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        raise StopRequested()
    except asyncio.TimeoutError:
        return


class RateLimiter:
    """
    Token Bucket с LazyLock — `asyncio.Lock` создаётся при первом acquire
    внутри активного event loop, что позволяет корректно работать при
    пересоздании цикла через `asyncio.run()`.
    """

    def __init__(self, rate: float, burst: int = 1) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate: float = float(rate)
        self.max_tokens: float = float(burst)
        self.tokens: float = float(burst)
        self.last_refill: float = time.monotonic()
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
        self.last_refill = now

    async def acquire(self, stop_event: Optional[asyncio.Event] = None) -> None:
        lock = self._ensure_lock()
        wait = 0.0
        async with lock:
            now = time.monotonic()
            self._refill_locked(now)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            deficit = 1.0 - self.tokens
            wait = deficit / self.rate
            self.tokens -= 1.0
        if wait > 0:
            await interruptible_wait(wait, stop_event)


# ================= CHECKER CORE =================
class CheckerCore:
    _NO_SESSION_MSG = "HTTP-сессия не инициализирована (нужен async with CheckerCore)"

    def __init__(
        self,
        config: Dict[str, Any],
        yandex_rps: float = 0.2,
        google_rps: float = 0.1,
        log_cb: Optional[Callable[[str], None]] = None,
        yandex_burst: int = 1,
        google_burst: int = 1,
    ) -> None:
        self.cfg: Dict[str, Any] = config
        self.log: Callable[[str], None] = log_cb or (lambda msg: None)
        self.yandex_limiter = RateLimiter(rate=yandex_rps, burst=yandex_burst)
        self.google_limiter = RateLimiter(rate=google_rps, burst=google_burst)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "CheckerCore":
        timeout = aiohttp.ClientTimeout(
            total=AIOHTTP_TOTAL_TIMEOUT_SEC,
            connect=AIOHTTP_CONNECT_TIMEOUT_SEC,
            sock_connect=AIOHTTP_SOCK_CONNECT_TIMEOUT_SEC,
            sock_read=AIOHTTP_SOCK_READ_TIMEOUT_SEC,
        )
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        session = self.session
        self.session = None
        if session is None:
            return

        cancelled: Optional[asyncio.CancelledError] = None
        try:
            await session.close()
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:
            logger.exception("Ошибка при закрытии aiohttp-сессии")

        try:
            await asyncio.shield(asyncio.sleep(AIOHTTP_CLOSE_GRACE_SEC))
        except asyncio.CancelledError:
            if cancelled is None:
                cancelled = asyncio.CancelledError()
        except RuntimeError:
            pass
        except Exception:
            logger.exception("Ошибка во время grace-паузы")

        if cancelled is not None:
            raise cancelled

    @staticmethod
    def _parse_yandex(
        data: Dict[str, Any], target_url: str,
    ) -> Tuple[Optional[bool], Optional[str]]:
        raw = data.get("rawData")
        if raw:
            try:
                xml_bytes = base64.b64decode(raw)
            except (ValueError, TypeError) as error:
                return None, f"Yandex base64: {type(error).__name__}"
            return CheckerCore._parse_yandex_xml(xml_bytes, target_url)
        return CheckerCore._search_in_json(data, target_url)

    @staticmethod
    def _parse_yandex_xml(
        xml_bytes: bytes, target_url: str,
    ) -> Tuple[Optional[bool], Optional[str]]:
        try:
            root = SafeET.fromstring(xml_bytes)
        except _DefusedXmlException as error:
            logger.warning("Yandex XML security rejection: %s", error)
            return None, f"Yandex XML security: {type(error).__name__}"
        except ET.ParseError as error:
            return None, f"Yandex XML ParseError: {error}"
        except (ValueError, TypeError) as error:
            return None, f"Yandex XML: {type(error).__name__}"

        target = normalize_url(target_url)

        def local(tag: str) -> str:
            return tag.split("}", 1)[1] if "}" in tag else tag

        for doc in root.iter():
            if local(doc.tag) != "doc":
                continue
            for child in doc:
                if local(child.tag) == "url" and child.text:
                    if normalize_url(child.text) == target:
                        return True, None
        return False, None

    @staticmethod
    def _search_in_json(
        data: Any, target_url: str,
    ) -> Tuple[Optional[bool], Optional[str]]:
        target = normalize_url(target_url)
        doc_containers = {"documents", "docs", "results", "items"}
        url_containers = {"urls"}

        stack: Deque[Any] = deque([data])
        visited_nodes = 0

        while stack:
            visited_nodes += 1
            if visited_nodes > MAX_JSON_TRAVERSAL_NODES:
                logger.warning(
                    "Yandex JSON traversal exceeded %d nodes — обход прерван",
                    MAX_JSON_TRAVERSAL_NODES,
                )
                return None, "Yandex: JSON слишком большой для обхода"

            node = stack.pop()
            if isinstance(node, dict):
                for key_raw, value in node.items():
                    key = key_raw.lower() if isinstance(key_raw, str) else ""
                    if key in url_containers and isinstance(value, list):
                        for item in value:
                            if isinstance(item, str) and normalize_url(item) == target:
                                return True, None
                            if isinstance(item, dict):
                                u = item.get("url")
                                if isinstance(u, str) and normalize_url(u) == target:
                                    return True, None
                    elif key in doc_containers and isinstance(value, list):
                        for doc in value:
                            if isinstance(doc, dict):
                                u = doc.get("url")
                                if isinstance(u, str) and normalize_url(u) == target:
                                    return True, None
                            if isinstance(doc, (dict, list)):
                                stack.append(doc)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        stack.append(item)

        return False, None

    # ---------- Yandex ----------
    async def check_yandex(
        self, url: str, stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[Optional[bool], Optional[str]]:
        api_key = ConfigManager.get_credential(self.cfg, "yandex_api_key", ENV_YANDEX_KEY)
        folder_id = ConfigManager.get_credential(self.cfg, "yandex_folder_id", ENV_YANDEX_FOLDER)
        if not api_key or not folder_id:
            return None, "Yandex API ключ не задан"

        session = self.session
        if session is None:
            return None, self._NO_SESSION_MSG

        await self.yandex_limiter.acquire(stop_event)

        endpoint = "https://searchapi.api.cloud.yandex.net/v2/web/search"
        headers = {
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": build_yandex_query(url),
            },
            "folderId": folder_id,
            "responseFormat": "FORMAT_XML",
        }

        try:
            async with session.post(endpoint, json=body, headers=headers) as resp:
                if resp.status == HTTP_TOO_MANY_REQUESTS:
                    await resp.read()
                    await interruptible_wait(RATE_LIMIT_RETRY_SEC, stop_event)
                    return None, "Yandex 429 (rate limit)"
                if resp.status == HTTP_FORBIDDEN:
                    return None, "Yandex 403 (проверьте ключ / права)"
                if resp.status == HTTP_UNAUTHORIZED:
                    return None, "Yandex 401 (неверный API-ключ)"
                if resp.status != HTTP_OK:
                    txt = await _safe_response_text(resp)
                    return None, f"Yandex HTTP {resp.status}: {txt}"
                try:
                    data = await resp.json()
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as error:
                    return None, f"Yandex: некорректный JSON ({type(error).__name__})"
                return self._parse_yandex(data, url)
        except (StopRequested, asyncio.CancelledError):
            raise
        except asyncio.TimeoutError as error:
            logger.exception("Yandex timeout для %s", url)
            return None, _format_error("Yandex", error)
        except aiohttp.ClientError as error:
            logger.exception("Yandex ClientError для %s", url)
            return None, _format_error("Yandex", error)
        except Exception as error:
            logger.exception("Yandex request failed for %s", url)
            return None, _format_error("Yandex", error)

    # ---------- Google ----------
    async def check_google(
        self, url: str, stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[Optional[bool], Optional[str]]:
        api_key = ConfigManager.get_credential(self.cfg, "google_api_key", ENV_GOOGLE_KEY)
        cx = ConfigManager.get_credential(self.cfg, "google_cx", ENV_GOOGLE_CX)
        if not api_key or not cx:
            return None, "Google API ключ не задан"

        session = self.session
        if session is None:
            return None, self._NO_SESSION_MSG

        await self.google_limiter.acquire(stop_event)

        endpoint = "https://www.googleapis.com/customsearch/v1"
        params = {"key": api_key, "cx": cx, "q": f"site:{url}", "num": 10}
        try:
            async with session.get(endpoint, params=params) as resp:
                if resp.status == HTTP_TOO_MANY_REQUESTS:
                    await resp.read()
                    await interruptible_wait(RATE_LIMIT_RETRY_SEC, stop_event)
                    return None, "Google 429 (rate limit)"
                if resp.status == HTTP_FORBIDDEN:
                    txt = await _safe_response_text(resp)
                    if "quota" in txt.lower():
                        return None, "Google 403: превышена дневная квота (100/день)"
                    return None, "Google 403 (проверьте ключ)"
                if resp.status == HTTP_BAD_REQUEST:
                    txt = await _safe_response_text(resp)
                    return None, f"Google 400 (неверный CX или ключ): {txt}"
                if resp.status != HTTP_OK:
                    txt = await _safe_response_text(resp)
                    return None, f"Google HTTP {resp.status}: {txt}"
                try:
                    data = await resp.json()
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as error:
                    return None, f"Google: некорректный JSON ({type(error).__name__})"
                return self._parse_google(data, url)
        except (StopRequested, asyncio.CancelledError):
            raise
        except asyncio.TimeoutError as error:
            logger.exception("Google timeout для %s", url)
            return None, _format_error("Google", error)
        except aiohttp.ClientError as error:
            logger.exception("Google ClientError для %s", url)
            return None, _format_error("Google", error)
        except Exception as error:
            logger.exception("Google request failed for %s", url)
            return None, _format_error("Google", error)

    @staticmethod
    def _parse_google(
        data: Dict[str, Any], target_url: str,
    ) -> Tuple[Optional[bool], Optional[str]]:
        items = data.get("items") or []
        target = normalize_url(target_url)
        for item in items:
            link = item.get("link", "")
            if link and normalize_url(link) == target:
                return True, None
        return False, None

    # ---------- Один URL ----------
    async def check_one(
        self,
        url: str,
        do_yandex: bool,
        do_google: bool,
        stop_event: Optional[asyncio.Event] = None,
    ) -> CheckResult:
        row: CheckResult = {
            "url": url, "yandex": None, "google": None, "errors": [],
        }

        if do_yandex:
            self.log(f"→ Yandex: {url}")
            ok, err = await self.check_yandex(url, stop_event)
            row["yandex"] = ok
            if err:
                row["errors"].append(err)
                self.log(f"  ! {err}")
            else:
                self.log(f"  ✓ Yandex: {'проиндексирован' if ok else 'не проиндексирован'}")

        if do_google:
            self.log(f"→ Google: {url}")
            ok, err = await self.check_google(url, stop_event)
            row["google"] = ok
            if err:
                row["errors"].append(err)
                self.log(f"  ! {err}")
            else:
                self.log(f"  ✓ Google: {'проиндексирован' if ok else 'не проиндексирован'}")

        return row


__all__ = ["RateLimiter", "interruptible_wait", "CheckerCore"]