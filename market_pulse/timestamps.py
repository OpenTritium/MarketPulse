"""UTC 时间戳的存储与解析约定。"""

from __future__ import annotations

from datetime import UTC, datetime

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(UTC)


def format_utc_timestamp(value: datetime) -> str:
    """将带时区的时间标准化为可按字典序比较的 UTC 文本。"""
    if value.tzinfo is None:
        raise ValueError("UTC 时间戳必须带时区")
    return value.astimezone(UTC).strftime(UTC_TIMESTAMP_FORMAT)


def parse_utc_timestamp(value: str) -> datetime:
    """解析 RFC 3339 UTC 格式及历史 SQLite UTC 文本格式。"""
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_optional_utc_timestamp(value: str | None) -> str | None:
    """标准化可解析的可选时间；空值或未知来源格式返回 ``None``。"""
    if value is None or not value.strip():
        return None
    try:
        return format_utc_timestamp(parse_utc_timestamp(value.strip()))
    except ValueError:
        return None
