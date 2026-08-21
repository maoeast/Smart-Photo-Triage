# ruff: noqa: E501
"""Local-only review service and persistent human decisions.

The long lines intentionally live inside embedded, dependency-free CSS and JavaScript assets.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from smart_photo_triage.database import connect_database
from smart_photo_triage.grouping import BURST_ALGORITHM_VERSION, DUPLICATE_ALGORITHM_VERSION
from smart_photo_triage.preprocess import PreviewError, _open_source_nofollow
from smart_photo_triage.workspace import Workspace

SCENE_CATEGORIES = (
    "01_家庭生活",
    "02_旅行风光",
    "03_工作与文档",
    "04_截图与备忘",
    "05_其他",
)
DISPOSITIONS = ("KEEP", "REVIEW", "REJECT_CANDIDATE")
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
MAX_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 4096
_ITEM_PATH = re.compile(r"^/api/items/([1-9][0-9]*)/decision$")
_PREVIEW_PATH = re.compile(r"^/api/preview/([1-9][0-9]*)$")


class ReviewError(RuntimeError):
    """Base error for a rejected local review operation."""


class ReviewConflictError(ReviewError):
    """Raised when an optimistic human-decision update is stale."""


class ReviewStateError(ReviewError):
    """Raised when persisted local review state violates its read contract."""

    def __init__(self) -> None:
        super().__init__("LOCAL_STATE_ERROR")


@dataclass(frozen=True, slots=True)
class ReviewPage:
    items: tuple[dict[str, object], ...]
    page: int
    page_size: int
    total: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _filename(path_text: object) -> str:
    return str(path_text).replace("\\", "/").rsplit("/", 1)[-1]


def _json_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str) or len(value) > 16_384:
        raise ReviewStateError
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReviewStateError from error
    if (
        not isinstance(parsed, list)
        or len(parsed) > 64
        or any(not isinstance(item, str) or len(item) > 500 for item in parsed)
    ):
        raise ReviewStateError
    return parsed


def _json_object(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, str) or len(value) > 16_384:
        raise ReviewStateError
    try:
        parsed = json.loads(value)
        normalized = json.dumps(parsed, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReviewStateError from error
    if not isinstance(parsed, dict) or len(normalized) > 16_384:
        raise ReviewStateError
    return parsed


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ReviewStateError
    return value


def _optional_number(value: object) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ReviewStateError
    return value


def _optional_text(value: object, *, maximum: int = 16_384) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ReviewStateError
    return value


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_EFFECTIVE_CTE = f"""
WITH latest_ai AS (
    SELECT *
    FROM (
        SELECT a.*,
               ROW_NUMBER() OVER (
                   PARTITION BY a.media_id
                   ORDER BY a.created_at DESC, a.id DESC
               ) AS latest_rank
        FROM ai_analysis AS a
    )
    WHERE latest_rank = 1
),
effective AS (
    SELECT
        m.id,
        spt_basename(m.original_path) AS filename,
        m.captured_at,
        p.preview_path,
        p.preview_status,
        p.preview_sha256,
        p.quality_json,
        p.quality_score AS local_quality_score,
        p.preview_width,
        p.preview_height,
        a.scene_category AS ai_category,
        a.disposition AS ai_disposition,
        a.confidence AS ai_confidence,
        a.quality_score AS ai_quality_score,
        a.provider AS ai_provider,
        a.model AS ai_model,
        a.tags_json,
        a.short_desc,
        a.reason AS ai_reason,
        COALESCE(r.scene_category, a.scene_category, '05_其他') AS effective_category,
        COALESCE(r.disposition, a.disposition, 'REVIEW') AS effective_disposition,
        CASE
            WHEN r.media_id IS NOT NULL THEN 'HUMAN'
            WHEN a.id IS NOT NULL THEN 'AI'
            ELSE 'RULE'
        END AS decision_source,
        COALESCE(r.revision, 0) AS revision,
        (
            SELECT MIN(dm.group_id)
            FROM duplicate_member AS dm
            JOIN duplicate_group AS dg ON dg.id = dm.group_id
            WHERE dm.media_id = m.id
              AND dg.algorithm_version = {_sql_literal(DUPLICATE_ALGORITHM_VERSION)}
        ) AS duplicate_group,
        (
            SELECT MIN(bm.group_id)
            FROM burst_member AS bm
            JOIN burst_group AS bg ON bg.id = bm.group_id
            WHERE bm.media_id = m.id
              AND bg.algorithm_version = {_sql_literal(BURST_ALGORITHM_VERSION)}
        ) AS burst_group,
        COALESCE((
            SELECT MAX(bm.is_representative)
            FROM burst_member AS bm
            JOIN burst_group AS bg ON bg.id = bm.group_id
            WHERE bm.media_id = m.id
              AND bg.algorithm_version = {_sql_literal(BURST_ALGORITHM_VERSION)}
        ), 0) AS is_representative,
        COALESCE((
            SELECT MAX(bm.is_best_shot)
            FROM burst_member AS bm
            JOIN burst_group AS bg ON bg.id = bm.group_id
            WHERE bm.media_id = m.id
              AND bg.algorithm_version = {_sql_literal(BURST_ALGORITHM_VERSION)}
        ), 0) AS is_best_shot
    FROM media_item AS m
    LEFT JOIN media_preprocess AS p ON p.media_id = m.id
    LEFT JOIN latest_ai AS a ON a.media_id = m.id
    LEFT JOIN review_decision AS r ON r.media_id = m.id
    WHERE m.source_present = 1 AND m.media_type IN ('IMAGE', 'VIDEO')
)
"""


class ReviewStore:
    """Queries effective decisions and writes only HUMAN overrides."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def list_items(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        year: int | None = None,
        month: int | None = None,
        category: str | None = None,
        disposition: str | None = None,
        query: str | None = None,
        duplicate_group: str | None = None,
        burst_group: str | None = None,
    ) -> ReviewPage:
        if type(page) is not int or page < 1:
            raise ReviewError("page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ReviewError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if year is not None and (type(year) is not int or not 1 <= year <= 9999):
            raise ReviewError("year is invalid")
        if month is not None and (type(month) is not int or not 1 <= month <= 12):
            raise ReviewError("month is invalid")
        if category is not None and category not in SCENE_CATEGORIES:
            raise ReviewError("category is invalid")
        if disposition is not None and disposition not in DISPOSITIONS:
            raise ReviewError("disposition is invalid")
        if query is not None and (not isinstance(query, str) or len(query) > 200):
            raise ReviewError("search query is invalid")
        for label, value in (("duplicate group", duplicate_group), ("burst group", burst_group)):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200):
                raise ReviewError(f"{label} is invalid")

        clauses: list[str] = []
        parameters: list[object] = []
        if year is not None:
            clauses.append("substr(e.captured_at, 1, 4) = ?")
            parameters.append(f"{year:04d}")
        if month is not None:
            clauses.append("substr(e.captured_at, 6, 2) = ?")
            parameters.append(f"{month:02d}")
        if category is not None:
            clauses.append("e.effective_category = ?")
            parameters.append(category)
        if disposition is not None:
            clauses.append("e.effective_disposition = ?")
            parameters.append(disposition)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(e.filename LIKE ? ESCAPE '\\' OR e.tags_json LIKE ? ESCAPE '\\' "
                "OR e.short_desc LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern, pattern))
        if duplicate_group is not None:
            clauses.append("e.duplicate_group = ?")
            parameters.append(duplicate_group)
        if burst_group is not None:
            clauses.append("e.burst_group = ?")
            parameters.append(burst_group)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        connection = connect_database(self.workspace.database_path, read_only=True)
        connection.create_function("spt_basename", 1, _filename, deterministic=True)
        connection.row_factory = sqlite3.Row
        try:
            total_row = connection.execute(
                _EFFECTIVE_CTE + f"SELECT COUNT(*) AS item_count FROM effective AS e {where}",
                parameters,
            ).fetchone()
            total = int(total_row["item_count"] if total_row is not None else 0)
            rows = connection.execute(
                _EFFECTIVE_CTE
                + f"""
                  SELECT * FROM effective AS e
                  {where}
                  ORDER BY e.id
                  LIMIT ? OFFSET ?
                  """,
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        finally:
            connection.close()
        return ReviewPage(
            items=tuple(self._row_to_item(row) for row in rows),
            page=page,
            page_size=page_size,
            total=total,
        )

    def get_item(self, media_id: int) -> dict[str, object] | None:
        if type(media_id) is not int or media_id < 1:
            raise ReviewError("media id is invalid")
        connection = connect_database(self.workspace.database_path, read_only=True)
        connection.create_function("spt_basename", 1, _filename, deterministic=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                _EFFECTIVE_CTE + "SELECT * FROM effective AS e WHERE e.id = ?",
                (media_id,),
            ).fetchone()
        finally:
            connection.close()
        return self._row_to_item(row) if row is not None else None

    def _row_to_item(self, row: sqlite3.Row) -> dict[str, object]:
        try:
            media_id = _integer(row["id"], minimum=1)
            filename = _optional_text(row["filename"], maximum=4096)
            category = _optional_text(row["effective_category"], maximum=100)
            disposition = _optional_text(row["effective_disposition"], maximum=100)
            source = _optional_text(row["decision_source"], maximum=20)
            if not filename or category not in SCENE_CATEGORIES:
                raise ReviewStateError
            if disposition not in DISPOSITIONS or source not in {"HUMAN", "AI", "RULE"}:
                raise ReviewStateError
            ai_category = _optional_text(row["ai_category"], maximum=100)
            ai_disposition = _optional_text(row["ai_disposition"], maximum=100)
            if ai_category is not None and ai_category not in SCENE_CATEGORIES:
                raise ReviewStateError
            if ai_disposition is not None and ai_disposition not in DISPOSITIONS:
                raise ReviewStateError
            preview_status = _optional_text(row["preview_status"], maximum=30)
            preview_path = _optional_text(row["preview_path"], maximum=4096)
            preview_sha256 = _optional_text(row["preview_sha256"], maximum=128)
            preview_ready = preview_status == "READY" and bool(preview_path and preview_sha256)
            preview_width = row["preview_width"]
            preview_height = row["preview_height"]
            if preview_width is not None:
                preview_width = _integer(preview_width, minimum=1)
            if preview_height is not None:
                preview_height = _integer(preview_height, minimum=1)
            duplicate_group = _optional_text(row["duplicate_group"], maximum=200)
            burst_group = _optional_text(row["burst_group"], maximum=200)
            if duplicate_group == "" or burst_group == "":
                raise ReviewStateError
            representative = _integer(row["is_representative"])
            best_shot = _integer(row["is_best_shot"])
            if representative not in {0, 1} or best_shot not in {0, 1}:
                raise ReviewStateError
            return {
                "id": media_id,
                "filename": filename,
                "captured_at": _optional_text(row["captured_at"], maximum=100),
                "preview_url": f"/api/preview/{media_id}" if preview_ready else None,
                "preview_width": preview_width,
                "preview_height": preview_height,
                "effective_category": category,
                "effective_disposition": disposition,
                "decision_source": source,
                "human_overrides_ai": source == "HUMAN" and ai_disposition is not None,
                "revision": _integer(row["revision"]),
                "ai_category": ai_category,
                "ai_disposition": ai_disposition,
                "ai_confidence": _optional_number(row["ai_confidence"]),
                "ai_quality_score": _optional_number(row["ai_quality_score"]),
                "ai_provider": _optional_text(row["ai_provider"], maximum=200),
                "ai_model": _optional_text(row["ai_model"], maximum=500),
                "ai_reason": _optional_text(row["ai_reason"]),
                "tags": _json_list(row["tags_json"]),
                "short_desc": _optional_text(row["short_desc"]),
                "local_quality_score": _optional_number(row["local_quality_score"]),
                "quality": _json_object(row["quality_json"]),
                "duplicate_group": duplicate_group,
                "burst_group": burst_group,
                "is_representative": bool(representative),
                "is_best_shot": bool(best_shot),
            }
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ReviewStateError from error

    def update_decision(
        self,
        media_id: int,
        *,
        category: str,
        disposition: str,
        expected_revision: int,
    ) -> dict[str, object]:
        if type(media_id) is not int or media_id < 1:
            raise ReviewError("media id is invalid")
        if category not in SCENE_CATEGORIES:
            raise ReviewError("category is invalid")
        if disposition not in DISPOSITIONS:
            raise ReviewError("disposition is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ReviewError("expected revision is invalid")

        connection = connect_database(self.workspace.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            media = connection.execute(
                """
                SELECT 1 FROM media_item
                WHERE id = ? AND source_present = 1 AND media_type IN ('IMAGE', 'VIDEO')
                """,
                (media_id,),
            ).fetchone()
            if media is None:
                raise ReviewError("review item was not found")
            current = connection.execute(
                "SELECT revision FROM review_decision WHERE media_id = ?",
                (media_id,),
            ).fetchone()
            current_revision = _integer(current[0]) if current is not None else 0
            if current_revision != expected_revision:
                raise ReviewConflictError("decision changed; reload before saving")
            now = _utc_now()
            if current is None:
                connection.execute(
                    """
                    INSERT INTO review_decision(
                        media_id,scene_category,disposition,decision_source,
                        revision,created_at,updated_at
                    ) VALUES (?,?,?,'HUMAN',1,?,?)
                    """,
                    (media_id, category, disposition, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE review_decision
                    SET scene_category = ?, disposition = ?, decision_source = 'HUMAN',
                        revision = revision + 1, updated_at = ?
                    WHERE media_id = ?
                    """,
                    (category, disposition, now, media_id),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        item = self.get_item(media_id)
        if item is None:  # pragma: no cover - protected by the write transaction
            raise ReviewError("review item disappeared after update")
        return item

    def read_preview(self, media_id: int) -> tuple[str, bytes]:
        if type(media_id) is not int or media_id < 1:
            raise ReviewError("media id is invalid")
        connection = connect_database(self.workspace.database_path, read_only=True)
        try:
            row = connection.execute(
                """
                SELECT p.preview_path,p.preview_sha256
                FROM media_item AS m
                JOIN media_preprocess AS p ON p.media_id = m.id
                WHERE m.id = ? AND m.source_present = 1
                  AND m.media_type IN ('IMAGE','VIDEO') AND p.preview_status = 'READY'
                """,
                (media_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row[0] or not row[1]:
            raise ReviewError("preview was not found")

        root = Path(os.path.abspath(self.workspace.root / "previews"))
        path = Path(os.path.abspath(Path(str(row[0]))))
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise ReviewError("preview is outside controlled previews") from error
        if not relative.parts:
            raise ReviewError("preview is outside controlled previews")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        try:
            with _open_source_nofollow(path, root, share_delete=False) as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_PREVIEW_BYTES:
                    raise ReviewError("preview is not a bounded regular file")
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_PREVIEW_BYTES:
                        raise ReviewError("preview exceeds the response bound")
                    digest.update(chunk)
                    chunks.append(chunk)
        except (OSError, PreviewError, ValueError) as error:
            raise ReviewError("preview cannot be opened safely") from error
        if not secrets.compare_digest(digest.hexdigest(), str(row[1])):
            raise ReviewError("preview content identity changed")
        mime_type = mimetypes.guess_type(path.name)[0]
        if mime_type not in {"image/webp", "image/jpeg", "image/png"}:
            mime_type = "image/webp"
        return mime_type, b"".join(chunks)


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Smart Photo Triage Review</title>
  <link rel="stylesheet" href="/assets/app.css">
</head>
<body>
  <header><h1>Smart Photo Triage</h1><p>本地复核。人工决定始终优先。</p></header>
  <main>
    <form id="filters" class="filters">
      <label>年份 <input id="year" inputmode="numeric" placeholder="2024"></label>
      <label>月份 <select id="month"><option value="">全部</option></select></label>
      <label>分类 <select id="category"><option value="">全部</option></select></label>
      <label>处置 <select id="disposition"><option value="">全部</option></select></label>
      <label>搜索 <input id="query" maxlength="200" placeholder="文件名、标签、描述"></label>
      <button type="submit">筛选</button>
    </form>
    <section id="group-filter" class="group-filter" hidden></section>
    <p id="status" role="status">正在加载…</p>
    <section id="grid" class="grid" aria-label="复核项目"></section>
    <nav class="pager" aria-label="分页">
      <button id="previous" type="button">上一页</button>
      <span id="page-label"></span>
      <button id="next" type="button">下一页</button>
    </nav>
  </main>
  <dialog id="lightbox">
    <button id="lightbox-close" type="button" aria-label="关闭">关闭</button>
    <img id="lightbox-image" alt="">
    <h2 id="lightbox-title"></h2>
    <p id="lightbox-detail"></p>
  </dialog>
  <script src="/assets/app.js" defer></script>
</body>
</html>
"""

_APP_CSS = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #111827; color: #f9fafb; }
header, main { max-width: 1500px; margin: auto; padding: 1rem; }
header h1 { margin-bottom: .25rem; }
.filters { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: .75rem; align-items: end; }
label { display: grid; gap: .3rem; }
input, select, button { min-height: 2.75rem; border-radius: .5rem; border: 1px solid #64748b; padding: .4rem .6rem; font: inherit; }
button { cursor: pointer; background: #1d4ed8; color: white; }
.group-filter { margin: 1rem 0; padding: .75rem; background: #1e293b; border-radius: .5rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(260px,1fr)); gap: 1rem; }
.card { background: #1f2937; border: 1px solid #475569; border-radius: .75rem; overflow: hidden; padding-bottom: .75rem; }
.card > img { width: 100%; height: 190px; object-fit: contain; background: #030712; cursor: zoom-in; }
.card-body { padding: 0 .75rem; display: grid; gap: .5rem; }
.card h2 { font-size: 1rem; overflow-wrap: anywhere; }
.badges { display: flex; gap: .35rem; flex-wrap: wrap; }
.badge { padding: .2rem .4rem; border-radius: .35rem; background: #334155; font-size: .8rem; }
.decision { display: grid; gap: .4rem; }
.quick { display: flex; gap: .4rem; flex-wrap: wrap; }
.quick button { flex: 1; }
.pager { display: flex; justify-content: center; align-items: center; gap: 1rem; padding: 1.5rem; }
dialog { width: min(92vw,1100px); background: #111827; color: #f9fafb; border: 1px solid #64748b; border-radius: .75rem; }
dialog img { display: block; max-width: 100%; max-height: 70vh; margin: auto; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (prefers-color-scheme: light) { body { background: #f8fafc; color: #0f172a; } .card { background: white; } }
"""

_APP_JS = r"""
"use strict";
const state = { page: 1, total: 0, pageSize: 100, csrf: "", categories: [], dispositions: [], duplicateGroup: "", burstGroup: "" };
const byId = (id) => document.getElementById(id);
const addOption = (select, value, label = value) => { const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option); };
const badge = (text) => { const span = document.createElement("span"); span.className = "badge"; span.textContent = text; return span; };

function setGroup(kind, value) {
  state.duplicateGroup = kind === "duplicate" ? value : "";
  state.burstGroup = kind === "burst" ? value : "";
  state.page = 1;
  const panel = byId("group-filter");
  panel.replaceChildren(document.createTextNode(`${kind === "duplicate" ? "精确重复" : "连拍"}组：${value} `));
  const clear = document.createElement("button"); clear.type = "button"; clear.textContent = "清除组筛选";
  clear.addEventListener("click", () => { state.duplicateGroup = ""; state.burstGroup = ""; panel.hidden = true; loadItems(); });
  panel.append(clear); panel.hidden = false; loadItems();
}

function openLightbox(item) {
  byId("lightbox-image").src = item.preview_url || "";
  byId("lightbox-image").alt = item.filename;
  byId("lightbox-title").textContent = item.filename;
  byId("lightbox-detail").textContent = `提供方：${item.ai_provider || "无"}。模型：${item.ai_model || "无"}。AI 原因：${item.ai_reason || "无"}。置信度：${item.ai_confidence ?? "无"}。本地质量：${JSON.stringify(item.quality)}`;
  byId("lightbox").showModal();
}

async function saveDecision(item, category, disposition, message) {
  const response = await fetch(`/api/items/${item.id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SPT-CSRF": state.csrf },
    body: JSON.stringify({ category, disposition, expected_revision: item.revision })
  });
  if (response.status === 409) { byId("status").textContent = "项目已被其他页面修改，已重新加载。"; await loadItems(); return; }
  if (!response.ok) throw new Error(`保存失败 (${response.status})`);
  Object.assign(item, await response.json());
  byId("status").textContent = message;
  await loadItems();
}

function renderCard(item) {
  const article = document.createElement("article"); article.className = "card"; article.dataset.mediaId = String(item.id);
  const image = document.createElement("img"); image.loading = "lazy"; image.src = item.preview_url || ""; image.alt = item.filename;
  image.addEventListener("click", () => openLightbox(item)); article.append(image);
  const body = document.createElement("div"); body.className = "card-body";
  const title = document.createElement("h2"); title.textContent = item.filename; body.append(title);
  const badges = document.createElement("div"); badges.className = "badges";
  badges.append(
    badge(item.human_overrides_ai ? "HUMAN 覆盖 AI" : item.decision_source),
    badge(item.effective_disposition)
  );
  if (item.is_best_shot) badges.append(badge("Best Shot"));
  if (item.is_representative) badges.append(badge("Representative"));
  body.append(badges);
  const description = document.createElement("p"); description.textContent = item.short_desc || "无 AI 描述"; body.append(description);
  const reason = document.createElement("p"); reason.textContent = `AI 原因：${item.ai_reason || "无"}`; body.append(reason);
  const confidence = document.createElement("p"); confidence.textContent = `置信度：${item.ai_confidence ?? "无"}。质量：${item.local_quality_score ?? "无"}`; body.append(confidence);
  const provider = document.createElement("p"); provider.textContent = `提供方：${item.ai_provider || "无"}。模型：${item.ai_model || "无"}`; body.append(provider);
  const groupButtons = document.createElement("div"); groupButtons.className = "quick";
  if (item.duplicate_group) { const button = document.createElement("button"); button.type = "button"; button.textContent = "查看精确重复组"; button.addEventListener("click", () => setGroup("duplicate", item.duplicate_group)); groupButtons.append(button); }
  if (item.burst_group) { const button = document.createElement("button"); button.type = "button"; button.textContent = "查看连拍组"; button.addEventListener("click", () => setGroup("burst", item.burst_group)); groupButtons.append(button); }
  body.append(groupButtons);
  const decision = document.createElement("div"); decision.className = "decision";
  const category = document.createElement("select"); category.setAttribute("aria-label", "人工分类"); state.categories.forEach((value) => addOption(category, value)); category.value = item.effective_category;
  const disposition = document.createElement("select"); disposition.setAttribute("aria-label", "人工处置"); state.dispositions.forEach((value) => addOption(disposition, value)); disposition.value = item.effective_disposition;
  const save = document.createElement("button"); save.type = "button"; save.textContent = "保存人工决定"; save.addEventListener("click", () => saveDecision(item, category.value, disposition.value, `已保存 ${item.filename}`));
  const quick = document.createElement("div"); quick.className = "quick";
  const keep = document.createElement("button"); keep.type = "button"; keep.textContent = "标记保留"; keep.addEventListener("click", () => saveDecision(item, category.value, "KEEP", `已保留 ${item.filename}`));
  const reject = document.createElement("button"); reject.type = "button"; reject.textContent = "标记淘汰候选"; reject.addEventListener("click", () => saveDecision(item, category.value, "REJECT_CANDIDATE", `已标记 ${item.filename}`));
  quick.append(keep, reject); decision.append(category, disposition, save, quick); body.append(decision); article.append(body); return article;
}

async function loadItems() {
  const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
  for (const [key, id] of [["year","year"],["month","month"],["category","category"],["disposition","disposition"],["q","query"]]) { const value = byId(id).value.trim(); if (value) params.set(key, value); }
  if (state.duplicateGroup) params.set("duplicate_group", state.duplicateGroup);
  if (state.burstGroup) params.set("burst_group", state.burstGroup);
  const response = await fetch(`/api/items?${params}`); if (!response.ok) throw new Error(`加载失败 (${response.status})`);
  const page = await response.json(); state.total = page.total;
  const grid = byId("grid"); grid.replaceChildren(...page.items.map(renderCard));
  const pages = Math.max(1, Math.ceil(page.total / page.page_size)); byId("page-label").textContent = `第 ${page.page} / ${pages} 页，共 ${page.total} 项`;
  byId("previous").disabled = page.page <= 1; byId("next").disabled = page.page >= pages;
  byId("status").textContent = `当前仅渲染 ${page.items.length} 项`;
}

async function start() {
  const response = await fetch("/api/bootstrap"); const bootstrap = await response.json();
  state.csrf = bootstrap.csrf_token; state.categories = bootstrap.categories; state.dispositions = bootstrap.dispositions; state.pageSize = bootstrap.page_size;
  for (let month = 1; month <= 12; month += 1) addOption(byId("month"), String(month), String(month));
  state.categories.forEach((value) => addOption(byId("category"), value)); state.dispositions.forEach((value) => addOption(byId("disposition"), value));
  byId("filters").addEventListener("submit", (event) => { event.preventDefault(); state.page = 1; loadItems().catch(showError); });
  byId("previous").addEventListener("click", () => { state.page -= 1; loadItems().catch(showError); });
  byId("next").addEventListener("click", () => { state.page += 1; loadItems().catch(showError); });
  byId("lightbox-close").addEventListener("click", () => byId("lightbox").close());
  await loadItems();
}
function showError(error) { byId("status").textContent = error instanceof Error ? error.message : "未知错误"; }
start().catch(showError);
"""


class ReviewHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying only workspace-local review state."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace: Workspace) -> None:
        self.store = ReviewStore(workspace)
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _host_is_valid(self) -> bool:
        return (
            self.headers.get("Host", "").casefold() == urlsplit(self._base_url()).netloc.casefold()
        )

    def _send(
        self,
        status_code: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status_code: int, payload: object) -> None:
        self._send(
            status_code,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def _error(self, status_code: int, code: str) -> None:
        self._json(status_code, {"error": code})

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_is_valid():
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_HOST")
            return
        target = urlsplit(self.path)
        if target.path == "/":
            self._send(HTTPStatus.OK, _INDEX_HTML.encode(), "text/html; charset=utf-8")
            return
        if target.path == "/assets/app.css":
            self._send(HTTPStatus.OK, _APP_CSS.encode(), "text/css; charset=utf-8")
            return
        if target.path == "/assets/app.js":
            self._send(HTTPStatus.OK, _APP_JS.encode(), "text/javascript; charset=utf-8")
            return
        if target.path == "/api/bootstrap":
            self._json(
                HTTPStatus.OK,
                {
                    "csrf_token": self.server.csrf_token,
                    "categories": SCENE_CATEGORIES,
                    "dispositions": DISPOSITIONS,
                    "page_size": DEFAULT_PAGE_SIZE,
                },
            )
            return
        if target.path == "/api/items":
            try:
                page = self._items_from_query(target.query)
            except ReviewStateError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LOCAL_STATE_ERROR")
                return
            except ReviewError:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_QUERY")
                return
            except sqlite3.Error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LOCAL_STATE_ERROR")
                return
            self._json(
                HTTPStatus.OK,
                {
                    "items": page.items,
                    "page": page.page,
                    "page_size": page.page_size,
                    "total": page.total,
                },
            )
            return
        match = _PREVIEW_PATH.fullmatch(target.path)
        if match is not None and not target.query:
            try:
                content_type, body = self.server.store.read_preview(int(match.group(1)))
            except ReviewError:
                self._error(HTTPStatus.NOT_FOUND, "PREVIEW_NOT_AVAILABLE")
                return
            except sqlite3.Error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LOCAL_STATE_ERROR")
                return
            self._send(HTTPStatus.OK, body, content_type)
            return
        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND")

    def _items_from_query(self, query: str) -> ReviewPage:
        allowed = {
            "page",
            "page_size",
            "year",
            "month",
            "category",
            "disposition",
            "q",
            "duplicate_group",
            "burst_group",
        }
        try:
            parsed = parse_qs(query, keep_blank_values=False, strict_parsing=True) if query else {}
        except ValueError as error:
            raise ReviewError("query parameters are invalid") from error
        if set(parsed) - allowed or any(len(values) != 1 for values in parsed.values()):
            raise ReviewError("query parameters are invalid")

        def integer(name: str, default: int | None = None) -> int | None:
            values = parsed.get(name)
            if values is None:
                return default
            try:
                return int(values[0])
            except ValueError as error:
                raise ReviewError(f"{name} is invalid") from error

        return self.server.store.list_items(
            page=cast(int, integer("page", 1)),
            page_size=cast(int, integer("page_size", DEFAULT_PAGE_SIZE)),
            year=integer("year"),
            month=integer("month"),
            category=parsed.get("category", [None])[0],
            disposition=parsed.get("disposition", [None])[0],
            query=parsed.get("q", [None])[0],
            duplicate_group=parsed.get("duplicate_group", [None])[0],
            burst_group=parsed.get("burst_group", [None])[0],
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_is_valid():
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_HOST")
            return
        target = urlsplit(self.path)
        match = _ITEM_PATH.fullmatch(target.path)
        if match is None or target.query:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
            return
        if self.headers.get("Origin") != self._base_url():
            self._error(HTTPStatus.FORBIDDEN, "INVALID_ORIGIN")
            return
        supplied = self.headers.get("X-SPT-CSRF", "")
        if not secrets.compare_digest(supplied, self.server.csrf_token):
            self._error(HTTPStatus.FORBIDDEN, "INVALID_CSRF")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON_REQUIRED")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "CONTENT_LENGTH_REQUIRED")
            return
        if not 1 <= length <= MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "REQUEST_TOO_LARGE")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_JSON")
            return
        if not isinstance(payload, dict) or set(payload) != {
            "category",
            "disposition",
            "expected_revision",
        }:
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_DECISION")
            return
        try:
            item = self.server.store.update_decision(
                int(match.group(1)),
                category=payload["category"],
                disposition=payload["disposition"],
                expected_revision=payload["expected_revision"],
            )
        except ReviewConflictError:
            self._error(HTTPStatus.CONFLICT, "STALE_DECISION")
            return
        except ReviewStateError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LOCAL_STATE_ERROR")
            return
        except ReviewError:
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_DECISION")
            return
        except sqlite3.Error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LOCAL_STATE_ERROR")
            return
        self._json(HTTPStatus.OK, item)


def create_review_server(
    workspace: Workspace,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ReviewHTTPServer:
    """Create an injectable short-lived local server without opening a browser."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("review server host must be a numeric loopback address") from error
    if not address.is_loopback or address.version != 4 or str(address) != "127.0.0.1":
        raise ValueError("review server may bind only to the 127.0.0.1 loopback address")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("review server port is invalid")
    return ReviewHTTPServer((host, port), workspace)


def serve_review(
    workspace: Workspace,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Run the local review service until interrupted."""
    server = create_review_server(workspace, host=host, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Review UI: {url}")
    try:
        if open_browser:
            webbrowser.open(url, new=2)
        with suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        server.server_close()
