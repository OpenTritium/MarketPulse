"""Turso/libSQL 存储层：事件时间线模型。

- events：合并后的事件实体（时间线条目）
- reports：同一事件的多个来源报道
- event_embeddings：固定 512 维的事件摘要向量

所有内部时间戳统一存为可按字典序比较的 RFC 3339 UTC 文本。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import zlib
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import libsql_experimental as _libsql
from uuid6 import uuid7

from .config import EMBEDDING_DIM, Config
from .timestamps import (
    format_utc_timestamp,
    normalize_optional_utc_timestamp,
    parse_utc_timestamp,
    utc_now,
)

_UTC_TIMESTAMP_GLOB = "????-??-??T??:??:??.??????Z"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT,
  headline TEXT,
  sentiment REAL,
  impact REAL,
  factors TEXT,
  related_symbols TEXT,
  category TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  report_count INTEGER NOT NULL DEFAULT 1,
  impact_sum REAL NOT NULL DEFAULT 0,
  published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_first_seen ON events(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_last_seen ON events(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  url TEXT NOT NULL,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TEXT,
  collected_at TEXT NOT NULL,
  raw_text TEXT,
  content_hash TEXT NOT NULL,
  sentiment REAL,
  impact REAL,
  factors TEXT,
  UNIQUE (url, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_reports_event ON reports(event_id);
CREATE INDEX IF NOT EXISTS idx_reports_url ON reports(url);

CREATE TABLE IF NOT EXISTS event_embeddings (
  event_id TEXT PRIMARY KEY REFERENCES events(id),
  embedding F32_BLOB({EMBEDDING_DIM})
);
CREATE INDEX IF NOT EXISTS emb_idx ON event_embeddings (libsql_vector_idx(embedding));
"""


def _connect(url: str) -> Any:
    """libsql-experimental 是无类型存根的 C 扩展。"""
    return cast(Any, _libsql).connect(url)


def _content_hash(title: str, raw_text: str, url: str) -> str:
    """计算标题、正文和 URL 的内容指纹。"""
    return hashlib.sha256(f"{url}\n{title}\n{raw_text}".encode()).hexdigest()


def try_json(value: str | None) -> Any | None:
    """容错 JSON 解析：脏数据返回 None。"""
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _vec_json(vector: list[float]) -> str:
    """向量转为 libSQL `vector32` 的 JSON 数组入参。"""
    try:
        return "[" + ",".join(repr(float(value)) for value in vector) + "]"
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding 包含无法转换为浮点数的值") from exc


@dataclass
class Report:
    """一条来源报道（采集产出，未入库）。"""

    url: str
    source: str
    title: str
    published_at: str | None
    raw_text: str
    summary: str | None
    headline: str | None
    sentiment: float | None
    impact: float | None
    factors: dict[str, float] = field(default_factory=dict)
    related_symbols: list[dict[str, str]] = field(default_factory=list)


def _ensure_local_dir(url: str) -> None:
    """为 file: URL 创建父目录。"""
    if not url.startswith("file:"):
        return
    path = url[5:]
    path = path.removeprefix("//")
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"无法创建数据库目录：{parent}") from exc


class Store:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self._reserved_hashes: set[str] = set()
        _ensure_local_dir(cfg.turso_url)
        self.conn: Any = _connect(cfg.turso_url)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self._validate_embedding_schema()
            self._migrate_impact_sum()
            self._migrate_report_scores()
            self._migrate_events_published_at()
            self._migrate_timestamps()
            self.conn.commit()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.conn.rollback()
            self.close()
            raise RuntimeError(f"数据库初始化失败：{exc}") from exc

    def _validate_embedding_schema(self) -> None:
        """拒绝与固定 embedding 契约不兼容的已有数据库。"""
        columns = self.conn.execute("PRAGMA table_info(event_embeddings)").fetchall()
        embedding_column = next((row for row in columns if row[1] == "embedding"), None)
        expected_type = f"F32_BLOB({EMBEDDING_DIM})"
        if embedding_column is None or embedding_column[2] != expected_type:
            raise RuntimeError(
                "当前数据库的向量维度与固定模型不兼容；请执行显式数据迁移或重建数据库"
            )

    def _migrate_impact_sum(self) -> None:
        """旧库补齐 impact_sum 列（impact 加权聚合需要）。"""
        columns = self.conn.execute("PRAGMA table_info(events)").fetchall()
        if any(row[1] == "impact_sum" for row in columns):
            return
        self.conn.execute(
            "ALTER TABLE events ADD COLUMN impact_sum REAL NOT NULL DEFAULT 0"
        )

    def _migrate_report_scores(self) -> None:
        """旧库补齐 reports 的报道级分数列（sentiment/impact/factors）。"""
        columns = self.conn.execute("PRAGMA table_info(reports)").fetchall()
        names = {row[1] for row in columns}
        for name, ddl in (
            ("sentiment", "REAL"),
            ("impact", "REAL"),
            ("factors", "TEXT"),
        ):
            if name not in names:
                self.conn.execute(f"ALTER TABLE reports ADD COLUMN {name} {ddl}")

    def _migrate_events_published_at(self) -> None:
        """旧库补齐 events.published_at 列（时间线按报道发布时间展示）。"""
        columns = self.conn.execute("PRAGMA table_info(events)").fetchall()
        if any(row[1] == "published_at" for row in columns):
            return
        self.conn.execute("ALTER TABLE events ADD COLUMN published_at TEXT")

    def _migrate_timestamps(self) -> None:
        """把历史 SQLite UTC 文本升级为统一 RFC 3339 UTC 格式。"""
        event_rows = self.conn.execute(
            """SELECT id, first_seen_at, last_seen_at FROM events
               WHERE first_seen_at NOT GLOB ? OR last_seen_at NOT GLOB ?""",
            (_UTC_TIMESTAMP_GLOB, _UTC_TIMESTAMP_GLOB),
        ).fetchall()
        for event_id, first_seen_at, last_seen_at in event_rows:
            normalized_first_seen = format_utc_timestamp(
                parse_utc_timestamp(first_seen_at)
            )
            normalized_last_seen = format_utc_timestamp(
                parse_utc_timestamp(last_seen_at)
            )
            self.conn.execute(
                """UPDATE events SET first_seen_at = ?, last_seen_at = ?
                   WHERE id = ?""",
                (normalized_first_seen, normalized_last_seen, event_id),
            )

        report_rows = self.conn.execute(
            """SELECT id, collected_at, published_at FROM reports
               WHERE collected_at NOT GLOB ?
                  OR (published_at IS NOT NULL AND published_at NOT GLOB ?)""",
            (_UTC_TIMESTAMP_GLOB, _UTC_TIMESTAMP_GLOB),
        ).fetchall()
        for report_id, collected_at, published_at in report_rows:
            normalized_collected_at = format_utc_timestamp(
                parse_utc_timestamp(collected_at)
            )
            normalized_published_at = normalize_optional_utc_timestamp(published_at)
            self.conn.execute(
                """UPDATE reports SET collected_at = ?, published_at = ?
                   WHERE id = ?""",
                (normalized_collected_at, normalized_published_at, report_id),
            )

    # ---- 去重（分析前置，避免重复调 LLM）----

    def is_known(self, url: str, title: str, raw_text: str) -> bool:
        """判断 URL+内容是否已经入库。"""
        content_hash = _content_hash(title, raw_text, url)
        row = self.conn.execute(
            "SELECT 1 FROM reports WHERE url = ? AND content_hash = ?",
            (url, content_hash),
        ).fetchone()
        return row is not None

    def reserve_report(self, url: str, title: str, raw_text: str) -> bool:
        """预留本轮尚未处理的报道，避免并发任务重复调用 LLM。"""
        content_hash = _content_hash(title, raw_text, url)
        if content_hash in self._reserved_hashes or self.is_known(url, title, raw_text):
            return False
        self._reserved_hashes.add(content_hash)
        return True

    # ---- 合并检索 ----

    def find_event(
        self, embedding: list[float], distance_max: float = 0.25, window_hours: int = 48
    ) -> str | None:
        """返回时间窗内相似度达标的最近事件 ID。"""
        query = _vec_json(embedding)
        cutoff = format_utc_timestamp(utc_now() - timedelta(hours=window_hours))
        rows = self.conn.execute(
            """SELECT e.id FROM vector_top_k('emb_idx', ?, 5) AS vt
               JOIN event_embeddings ae ON ae.rowid = vt.id
               JOIN events e ON e.id = ae.event_id
               WHERE vector_distance_cos(ae.embedding, ?) < ?
                 AND e.last_seen_at >= ?
               ORDER BY vector_distance_cos(ae.embedding, ?)
               LIMIT 1""",
            (query, query, distance_max, cutoff, query),
        ).fetchall()
        return rows[0][0] if rows else None

    # ---- 写入（合并或新建）----

    def merge_or_create(
        self, report: Report, embedding: list[float]
    ) -> tuple[str, str]:
        """写入一条报道，返回 `(event_id, outcome)`，但不自行提交事务。"""
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding 维度错误：期望 {EMBEDDING_DIM}，实际 {len(embedding)}"
            )
        content_hash = _content_hash(report.title, report.raw_text, report.url)
        row = self.conn.execute(
            "SELECT event_id FROM reports WHERE url = ? AND content_hash = ?",
            (report.url, content_hash),
        ).fetchone()
        if row:
            return row[0], "duplicate"
        now = format_utc_timestamp(utc_now())
        event_id = self.find_event(embedding)
        if event_id:
            self._attach_report(event_id, report, content_hash, now)
            self._update_event_aggregates(event_id, report, now)
            return event_id, "merged"

        event_id = str(uuid7())
        self.conn.execute(
            """INSERT INTO events
               (id, title, summary, headline, sentiment, impact, factors,
                related_symbols, category, first_seen_at, last_seen_at, report_count,
                impact_sum, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                event_id,
                report.title,
                report.summary,
                report.headline,
                report.sentiment,
                report.impact,
                json.dumps(report.factors, ensure_ascii=False)
                if report.factors
                else None,
                json.dumps(report.related_symbols, ensure_ascii=False)
                if report.related_symbols
                else None,
                None,
                now,
                now,
                report.impact or 0.0,
                normalize_optional_utc_timestamp(report.published_at),
            ),
        )
        self._attach_report(event_id, report, content_hash, now)
        self._insert_embedding(event_id, embedding)
        return event_id, "new"

    def _attach_report(
        self, event_id: str, report: Report, content_hash: str, collected_at: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO reports
               (id, event_id, url, source, title, published_at, collected_at,
                raw_text, content_hash, sentiment, impact, factors)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid7()),
                event_id,
                report.url,
                report.source,
                report.title,
                normalize_optional_utc_timestamp(report.published_at),
                collected_at,
                zlib.compress(report.raw_text[:8000].encode()),  # 压缩存 BLOB，省 ~60%
                content_hash,
                report.sentiment,
                report.impact,
                json.dumps(report.factors, ensure_ascii=False)
                if report.factors
                else None,
            ),
        )

    def _insert_embedding(self, event_id: str, embedding: list[float]) -> None:
        self.conn.execute(
            "INSERT INTO event_embeddings (event_id, embedding) VALUES (?, vector32(?))",
            (event_id, _vec_json(embedding)),
        )

    def _update_event_aggregates(
        self, event_id: str, report: Report, updated_at: str
    ) -> None:
        """合并后按 impact 加权平均情绪和因子，并更新最后出现时间。"""
        row = self.conn.execute(
            "SELECT sentiment, impact, factors, report_count, impact_sum, published_at FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return
        old_sentiment, old_impact, old_factors_json, report_count, old_impact_sum, _old_published_at = row
        try:
            count = int(report_count or 1)
        except (TypeError, ValueError):
            count = 1
        report_impact = report.impact or 0.0
        new_impact_sum = (old_impact_sum or 0.0) + report_impact

        def _weighted(old: float | None, new: float) -> float:
            """impact 加权；权重和为 0（全部无影响报道）时回退简单平均。"""
            if new_impact_sum > 0:
                return (
                    (old or 0.0) * (old_impact_sum or 0.0) + new * report_impact
                ) / (new_impact_sum)
            return ((old or 0.0) * count + new) / (count + 1)

        new_sentiment = _weighted(old_sentiment, report.sentiment or 0.0)
        new_impact = _weighted(old_impact, report_impact)
        report_published = normalize_optional_utc_timestamp(report.published_at)
        new_published_at = min(
            filter(None, [row[5], report_published]),
            default=None,
        )
        old_factors = try_json(old_factors_json)
        merged_factors: dict[str, float] = {}
        for name in set(old_factors or {}) | set(report.factors):
            old_value = (
                old_factors.get(name, 0.0) if isinstance(old_factors, dict) else 0.0
            )
            try:
                old_factor = float(old_value)
                new_factor = float(report.factors.get(name, 0.0))
            except (TypeError, ValueError):
                old_factor, new_factor = 0.0, 0.0
            merged_factors[name] = _weighted(old_factor, new_factor)

        self.conn.execute(
            """UPDATE events SET sentiment = ?, impact = ?, factors = ?,
               last_seen_at = ?, report_count = report_count + 1, impact_sum = ?,
               published_at = ?
               WHERE id = ?""",
            (
                new_sentiment,
                new_impact,
                json.dumps(merged_factors, ensure_ascii=False),
                updated_at,
                new_impact_sum,
                new_published_at,
                event_id,
            ),
        )

    @contextlib.contextmanager
    def batch(self) -> Generator[None]:
        """在成功时一次提交多个事件写入。"""
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    @contextlib.contextmanager
    def savepoint(self) -> Generator[None]:
        """隔离批次中单条报道的写入错误。"""
        self.conn.execute("SAVEPOINT report")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK TO SAVEPOINT report")
            self.conn.execute("RELEASE SAVEPOINT report")
            raise
        else:
            self.conn.execute("RELEASE SAVEPOINT report")

    # ---- 查询 ----

    def timeline(
        self, limit: int = 50, starting_after: str | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        """时间线：事件按出现时间倒序，带来源数组和总记录数。

        返回 `(total, rows)`，rows 最多 `limit + 1` 条（调用方据此判断
        has_more）；`starting_after` 为上一页最后一条的事件 ID，基于
        (first_seen_at, id) 复合游标，翻页期间插入新事件也不会重复或跳条。
        """
        try:
            total_row = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()
            total = int(total_row[0]) if total_row else 0
        except Exception as exc:
            raise RuntimeError("读取事件总数失败") from exc
        if starting_after is not None:
            cursor = self.conn.execute(
                "SELECT COALESCE(published_at, first_seen_at) FROM events WHERE id = ?", (starting_after,)
            ).fetchone()
            if cursor is None:
                raise ValueError("游标不存在")
            cursor_at = cursor[0]
        else:
            cursor_at = None
        if cursor_at is None:
            rows = self.conn.execute(
                """SELECT e.id, e.title, e.summary, e.headline, e.sentiment, e.impact,
                          e.factors, e.related_symbols, e.category,
                          e.first_seen_at, e.last_seen_at, e.report_count, e.published_at,
                          (SELECT GROUP_CONCAT(DISTINCT r.source) FROM reports r
                           WHERE r.event_id = e.id) AS sources
                   FROM events e
                   ORDER BY COALESCE(e.published_at, e.first_seen_at) DESC, e.id DESC
                   LIMIT ?""",
                (limit + 1,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT e.id, e.title, e.summary, e.headline, e.sentiment, e.impact,
                          e.factors, e.related_symbols, e.category,
                          e.first_seen_at, e.last_seen_at, e.report_count, e.published_at,
                          (SELECT GROUP_CONCAT(DISTINCT r.source) FROM reports r
                           WHERE r.event_id = e.id) AS sources
                   FROM events e
                   WHERE COALESCE(e.published_at, e.first_seen_at) < ? OR (COALESCE(e.published_at, e.first_seen_at) = ? AND e.id < ?)
                   ORDER BY COALESCE(e.published_at, e.first_seen_at) DESC, e.id DESC
                   LIMIT ?""",
                (cursor_at, cursor_at, starting_after, limit + 1),
            ).fetchall()
        return total, _rows_to_dicts(
            [
                "id",
                "title",
                "summary",
                "headline",
                "sentiment",
                "impact",
                "factors",
                "related_symbols",
                "category",
                "first_seen_at",
                "last_seen_at",
                "report_count",
                "published_at",
                "sources",
            ],
            rows,
        )

    def sentiment_rows_since(self, since: datetime) -> list[dict[str, Any]]:
        """读取 UTC 时间窗口内供情绪聚合使用的事件字段。"""
        rows = self.conn.execute(
            "SELECT first_seen_at, sentiment, factors FROM events WHERE first_seen_at >= ?",
            (format_utc_timestamp(since),),
        ).fetchall()
        return [
            {
                "at": parse_utc_timestamp(row[0]),
                "sentiment": row[1],
                "factors": try_json(row[2]),
            }
            for row in rows
        ]

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        """事件详情及其多来源报道列表。"""
        row = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.headline, e.sentiment, e.impact,
                      e.factors, e.related_symbols, e.category,
                      e.first_seen_at, e.last_seen_at, e.report_count, e.published_at
               FROM events e WHERE e.id = ?""",
            (event_id,),
        ).fetchone()
        if not row:
            return None

        event = _rows_to_dicts(
            [
                "id",
                "title",
                "summary",
                "headline",
                "sentiment",
                "impact",
                "factors",
                "related_symbols",
                "category",
                "first_seen_at",
                "last_seen_at",
                "report_count",
                "published_at",
            ],
            [row],
        )[0]
        reports = self.conn.execute(
            """SELECT url, source, title, published_at, collected_at, raw_text
               FROM reports WHERE event_id = ? ORDER BY collected_at""",
            (event_id,),
        ).fetchall()
        event["reports"] = []
        for report in reports:
            raw = report[5]
            if isinstance(raw, bytes):
                try:
                    raw_text = zlib.decompress(raw).decode()
                except (zlib.error, ValueError):
                    raw_text = raw.decode(errors="replace")  # 兼容旧明文数据
            else:
                raw_text = raw
            item = dict(
                zip(
                    [
                        "url",
                        "source",
                        "title",
                        "published_at",
                        "collected_at",
                        "raw_text",
                    ],
                    report,
                    strict=True,
                )
            )
            item["raw_text"] = raw_text  # 覆盖为解压后的原文
            event["reports"].append(item)
        return event

    def search_similar(
        self, query: str, embedding: list[float], k: int = 10
    ) -> list[dict[str, Any]]:
        """混合检索：关键词（标题/摘要）+ 向量近邻，RRF 融合排序。

        专有名词（C919、ts_code 等）对小型 embedding 模型召回弱，
        关键词通道保证精确命中；向量通道兜底语义近义。
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding 维度错误：期望 {EMBEDDING_DIM}，实际 {len(embedding)}"
            )
        esc = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{esc}%"
        # 关键词通道：标题命中优先于仅摘要命中，同组内新事件在前
        kw_rows = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.sentiment, e.first_seen_at,
                       e.last_seen_at, e.published_at, e.factors,
                       (SELECT GROUP_CONCAT(DISTINCT r.source) FROM reports r
                        WHERE r.event_id = e.id) AS sources,
                       CASE WHEN e.title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END AS kw_grp
                FROM events e
                WHERE e.title LIKE ? ESCAPE '\\' OR e.summary LIKE ? ESCAPE '\\'
                ORDER BY kw_grp, e.first_seen_at DESC""",
            (pattern, pattern, pattern),
        ).fetchall()
        # 向量通道：取 2k 候选
        vec_rows = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.sentiment, e.first_seen_at,
                      e.last_seen_at, e.published_at, e.factors,
                      (SELECT GROUP_CONCAT(DISTINCT r.source) FROM reports r
                       WHERE r.event_id = e.id) AS sources
               FROM vector_top_k('emb_idx', ?, ?) AS vt
               JOIN event_embeddings ae ON ae.rowid = vt.id
               JOIN events e ON e.id = ae.event_id""",
            (_vec_json(embedding), k * 2),
        ).fetchall()
        # RRF 融合：score = Σ 1/(60 + rank)
        scores: dict[str, float] = {}
        order: list[str] = []
        for rank, row in enumerate(kw_rows):
            key = row[0]
            scores[key] = scores.get(key, 0.0) + 1.0 / (60.0 + rank)
            order.append(key)
        for rank, row in enumerate(vec_rows):
            key = row[0]
            scores[key] = scores.get(key, 0.0) + 1.0 / (60.0 + rank)
            if key not in scores:
                order.append(key)
        by_id = {row[0]: row for row in kw_rows}
        by_id.update({row[0]: row for row in vec_rows})
        ranked = sorted(order, key=lambda i: scores[i], reverse=True)[:k]
        rows = [by_id[i] for i in ranked]
        return _rows_to_dicts(
            [
                "id",
                "title",
                "summary",
                "sentiment",
                "first_seen_at",
                "last_seen_at",
                "published_at",
                "factors",
                "sources",
            ],
            rows,
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()


def _rows_to_dicts(cols: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    """查询行转字典，并容错解析 JSON 列。"""
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(zip(cols, row, strict=True))
        item["factors"] = try_json(item.get("factors"))
        item["related_symbols"] = try_json(item.get("related_symbols")) or []
        item["sources"] = (
            item.get("sources", "").split(",") if item.get("sources") else []
        )
        result.append(item)
    return result
