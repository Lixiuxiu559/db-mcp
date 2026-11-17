"""Token 到期时间的服务本地解析与精确截止检查。"""

import re
import time
from datetime import datetime, timezone

_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def normalize_expiration(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ValueError("到期时间须为包含时区的 RFC3339 时间或 null")
    try:
        stamp = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError):
        raise ValueError("到期时间须为包含时区的 RFC3339 时间或 null") from None


def expiration_epoch(value: object) -> float | None:
    normalized = normalize_expiration(value)
    return datetime.fromisoformat(normalized).timestamp() if normalized is not None else None


def is_expired(value: object) -> bool:
    """损坏的持久化值同样拒绝；只有 SQL NULL 表示长期有效。"""
    try:
        epoch = expiration_epoch(value)
        return epoch is not None and time.time() >= epoch
    except (ValueError, OverflowError, OSError):
        return True
