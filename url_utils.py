"""Утилиты URL: валидация, нормализация, дедупликация, генерация запросов."""

import urllib.parse
from typing import Any, Iterable, List, Tuple

from .config import MAX_URL_LENGTH


def is_valid_url(u: Any) -> bool:
    """Строгая проверка URL: http/https, наличие netloc и точки в hostname."""
    if not isinstance(u, str):
        return False
    s = u.strip()
    if not s or len(s) > MAX_URL_LENGTH:
        return False
    try:
        p = urllib.parse.urlparse(s)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.netloc:
        return False
    host = p.hostname or ""
    if not host:
        return False
    if "." not in host and host != "localhost":
        return False
    return True


def normalize_url(u: str) -> str:
    """Ключ для сравнения URL: без схемы, без www., без хвостового слэша."""
    s = u.strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip("/")


def build_yandex_query(url: str) -> str:
    """
    Запрос для проверки индексации конкретного URL.
    `host:` фильтрует только по домену, поэтому используем точный URL в кавычках.
    """
    return f'"{url}"'


def dedupe_urls(urls: Iterable[str]) -> Tuple[List[str], int]:
    """
    Удаляет дубликаты URL с сохранением исходного порядка.

    Сравнение выполняется по `normalize_url()` — регистр схемы/домена
    и завершающий `/` не считаются отличиями.

    Пустые строки пропускаются.

    Возвращает:
      • список уникальных строк (в исходной форме, без обрезки);
      • количество удалённых дубликатов (включая пустые строки не считаются).
    """
    seen: set = set()
    unique: List[str] = []
    duplicates = 0
    for raw in urls:
        stripped = raw.strip()
        if not stripped:
            continue
        key = normalize_url(stripped)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(stripped)
    return unique, duplicates


def merge_unique_urls(
    existing: Iterable[str], incoming: Iterable[str]
) -> Tuple[List[str], int]:
    """
    Объединяет два набора URL, удаляя дубликаты между ними.

    Ключи существующих строк учитываются один раз, затем добавляются только
    те строки из `incoming`, которых ещё не было.

    Возвращает:
      • объединённый список уникальных строк;
      • количество удалённых дубликатов во входящем наборе.
    """
    seen: set = set()
    merged: List[str] = []

    for raw in existing:
        stripped = raw.strip()
        if not stripped:
            continue
        key = normalize_url(stripped)
        if key in seen:
            continue
        seen.add(key)
        merged.append(stripped)

    duplicates = 0
    for raw in incoming:
        stripped = raw.strip()
        if not stripped:
            continue
        key = normalize_url(stripped)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        merged.append(stripped)

    return merged, duplicates


__all__ = [
    "is_valid_url",
    "normalize_url",
    "build_yandex_query",
    "dedupe_urls",
    "merge_unique_urls",
]