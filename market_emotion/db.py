"""Turso/libSQL 存储层：事件时间线模型。

- events：合并后的事件实体（时间线条目），uuid7 主键（时间有序）
- reports：同一事件的多个来源报道（source 数组从这聚合）
- event_embeddings：事件摘要向量（合并检索 + 语义搜索）

关键语义（本地实测验证）：
  - 写入必须显式 commit() 才落盘
  - 向量列 F32_BLOB(维度)，索引 libsql_vector_idx，查询 vector_top_k
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import libsql_experimental as _libsql

from .config import Config
from uuid6 import uuid7

_SCHEMA = """
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
  report_count INTEGER NOT NULL DEFAULT 1
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
  UNIQUE (url, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_reports_event ON reports(event_id);
CREATE INDEX IF NOT EXISTS idx_reports_url ON reports(url);

CREATE TABLE IF NOT EXISTS event_embeddings (
  event_id TEXT PRIMARY KEY REFERENCES events(id),
  embedding F32_BLOB(512)
);
CREATE INDEX IF NOT EXISTS emb_idx ON event_embeddings (libsql_vector_idx(embedding));
"""


def _connect(url: str) -> Any:
    """libsql_experimental 是 C 扩展无类型存根，cast 后访问不触发属性检查。"""
    return cast(Any, _libsql).connect(url)


def _content_hash(title: str, raw_text: str, url: str) -> str:
    """内容指纹：标题+正文+URL。"""
    return hashlib.sha256(f"{url}\n{title}\n{raw_text}".encode()).hexdigest()


def try_json(s: str | None):
    """容错 JSON 解析：脏数据返回 None。"""
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _vec_json(vec: list[float]) -> str:
    """向量 → JSON 数组字符串（vector32 入参格式）。"""
    try:
        return "[" + ",".join(repr(float(x)) for x in vec) + "]"
    except (TypeError, ValueError):
        return "[0.0]"


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
    related_symbols: list[str] = field(default_factory=list)


def _ensure_local_dir(url: str) -> None:
    """file: URL 的父目录不存在时自动创建（libsql 不会自动建目录）。"""
    if not url.startswith("file:"):
        return
    path = url[5:]
    if path.startswith("//"):
        path = path[2:]
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:  # 目录创建失败时由后续连接错误暴露
            _ = exc


class Store:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        _ensure_local_dir(cfg.turso_url)
        self.conn: Any = _connect(cfg.turso_url)
        self.conn.execute(
            "PRAGMA journal_mode=WAL"
        )  # 多进程并发（api 读 + collector 写）
        self.conn.executescript(_SCHEMA)  # 建表脚本（常量），executescript 语义匹配
        self.conn.commit()

    # ---- 去重（分析前置，避免重复调 LLM）----

    def is_known(self, url: str, title: str, raw_text: str) -> bool:
        """该 URL+内容是否已入库。"""
        h = _content_hash(title, raw_text, url)
        row = self.conn.execute(
            "SELECT 1 FROM reports WHERE url = ? AND content_hash = ?",
            (url, h),
        ).fetchone()
        return row is not None

    # ---- 合并检索 ----

    def find_event(
        self, embedding: list[float], distance_max: float = 0.25, window_hours: int = 48
    ) -> str | None:
        """向量检索最近 5 个事件，返回相似度达标且在时间窗内的最近事件 id。

        distance_max 为余弦距离阈值（0=完全相同，0.25 ≈ 相似度 0.75）。
        """
        q = _vec_json(embedding)
        rows = self.conn.execute(
            """SELECT e.id FROM vector_top_k('emb_idx', ?, 5) AS vt
               JOIN event_embeddings ae ON ae.rowid = vt.id
               JOIN events e ON e.id = ae.event_id
               WHERE vector_distance_cos(ae.embedding, ?) < ?
                 AND e.last_seen_at >= datetime('now', '-' || ? || ' hours')
               ORDER BY vector_distance_cos(ae.embedding, ?)
               LIMIT 1""",
            (q, q, distance_max, window_hours, q),
        ).fetchall()
        return rows[0][0] if rows else None

    # ---- 写入（合并或新建）----

    def merge_or_create(
        self, report: Report, embedding: list[float]
    ) -> tuple[str, str]:
        """入库一条报道：URL 去重 → 相似事件合并 → 否则新建事件。

        返回 (event_id, "duplicate" | "merged" | "new")。
        """
        h = _content_hash(report.title, report.raw_text, report.url)
        # 1. URL 去重（同 URL 同内容）
        row = self.conn.execute(
            "SELECT event_id FROM reports WHERE url = ? AND content_hash = ?",
            (report.url, h),
        ).fetchone()
        if row:
            return row[0], "duplicate"

        # 2. 相似事件合并
        event_id = self.find_event(embedding)
        if event_id:
            self._attach_report(event_id, report, h)
            self._update_event_aggregates(event_id, report)
            return event_id, "merged"

        # 3. 新建事件
        event_id = str(uuid7())
        self.conn.execute(
            """INSERT INTO events
               (id, title, summary, headline, sentiment, impact, factors,
                related_symbols, category, first_seen_at, last_seen_at, report_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), 1)""",
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
            ),
        )
        self._attach_report(event_id, report, h)
        self._insert_embedding(event_id, embedding)
        self.conn.commit()
        return event_id, "new"

    def _attach_report(self, event_id: str, report: Report, h: str) -> None:
        self.conn.execute(
            """INSERT INTO reports
               (id, event_id, url, source, title, published_at, collected_at, raw_text, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
            (
                str(uuid7()),
                event_id,
                report.url,
                report.source,
                report.title,
                report.published_at,
                report.raw_text[:8000],
                h,
            ),
        )

    def _insert_embedding(self, event_id: str, embedding: list[float]) -> None:
        self.conn.execute(
            "INSERT INTO event_embeddings (event_id, embedding) VALUES (?, vector32(?))",
            (event_id, _vec_json(embedding)),
        )

    def _update_event_aggregates(self, event_id: str, report: Report) -> None:
        """合并后滚动平均 sentiment/impact/factors，更新 last_seen_at。"""
        row = self.conn.execute(
            "SELECT sentiment, impact, factors, report_count FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return
        old_sent, old_impact, old_factors_json, n = row
        try:
            n = int(n or 1)
        except (TypeError, ValueError):
            n = 1
        new_sent = ((old_sent or 0.0) * n + (report.sentiment or 0.0)) / (n + 1)
        new_impact = ((old_impact or 0.0) * n + (report.impact or 0.0)) / (n + 1)
        old_factors = try_json(old_factors_json) or {}
        merged_factors: dict[str, float] = {}
        for k in set(old_factors) | set(report.factors):
            old_v = old_factors.get(k, 0.0) if isinstance(old_factors, dict) else 0.0
            try:
                old_f = float(old_v)
                new_f = float(report.factors.get(k, 0.0))
            except (TypeError, ValueError):
                old_f, new_f = 0.0, 0.0
            merged_factors[k] = (old_f * n + new_f) / (n + 1)
        self.conn.execute(
            """UPDATE events SET sentiment = ?, impact = ?, factors = ?,
               last_seen_at = datetime('now'), report_count = report_count + 1
               WHERE id = ?""",
            (
                new_sent,
                new_impact,
                json.dumps(merged_factors, ensure_ascii=False),
                event_id,
            ),
        )
        self.conn.commit()

    # ---- 查询 ----

    def timeline(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """时间线：事件按 first_seen_at 倒序，带 sources 数组。"""
        rows = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.headline, e.sentiment, e.impact,
                      e.factors, e.related_symbols, e.category,
                      e.first_seen_at, e.last_seen_at, e.report_count,
                      (SELECT GROUP_CONCAT(r.source, ',') FROM reports r
                       WHERE r.event_id = e.id) AS sources
               FROM events e
               ORDER BY e.first_seen_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return _rows_to_dicts(
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
                "sources",
            ],
            rows,
        )

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        """事件详情 + 报道列表（多来源）。"""
        row = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.headline, e.sentiment, e.impact,
                      e.factors, e.related_symbols, e.category,
                      e.first_seen_at, e.last_seen_at, e.report_count
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
            ],
            [row],
        )[0]
        reports = self.conn.execute(
            """SELECT url, source, title, published_at, collected_at
               FROM reports WHERE event_id = ? ORDER BY collected_at""",
            (event_id,),
        ).fetchall()
        event["reports"] = [
            dict(
                zip(
                    ["url", "source", "title", "published_at", "collected_at"],
                    r,
                    strict=True,
                )
            )
            for r in reports
        ]
        return event

    def search_similar(
        self, embedding: list[float], k: int = 10
    ) -> list[dict[str, Any]]:
        """语义检索：事件摘要向量最近邻。"""
        q = _vec_json(embedding)
        rows = self.conn.execute(
            """SELECT e.id, e.title, e.summary, e.sentiment, e.first_seen_at,
                      e.last_seen_at, e.factors,
                      (SELECT GROUP_CONCAT(r.source, ',') FROM reports r
                       WHERE r.event_id = e.id) AS sources
               FROM vector_top_k('emb_idx', ?, ?) AS vt
               JOIN event_embeddings ae ON ae.rowid = vt.id
               JOIN events e ON e.id = ae.event_id""",
            (q, k),
        ).fetchall()
        return _rows_to_dicts(
            [
                "id",
                "title",
                "summary",
                "sentiment",
                "first_seen_at",
                "last_seen_at",
                "factors",
                "sources",
            ],
            rows,
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()


def _rows_to_dicts(cols: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    """查询行 → dict 列表，JSON 列容错解析（缺列返回空值）。"""
    out = []
    for r in rows:
        d = dict(zip(cols, r, strict=True))
        d["factors"] = try_json(d.get("factors"))
        d["related_symbols"] = try_json(d.get("related_symbols")) or []
        d["sources"] = d.get("sources", "").split(",") if d.get("sources") else []
        out.append(d)
    return out
