from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from PIL import Image

import smart_photo_triage.cli as cli_module
import smart_photo_triage.database as database_module
import smart_photo_triage.review as review_module
from smart_photo_triage.cli import build_parser, main
from smart_photo_triage.database import MIGRATIONS, apply_migrations, connect_database
from smart_photo_triage.grouping import BURST_ALGORITHM_VERSION, DUPLICATE_ALGORITHM_VERSION
from smart_photo_triage.review import (
    ReviewConflictError,
    ReviewError,
    ReviewStore,
    create_review_server,
)
from smart_photo_triage.workspace import Workspace, initialize_workspace


def seed_review_items(workspace: Workspace, count: int) -> tuple[int, ...]:
    preview_root = workspace.root / "previews"
    ids: list[int] = []
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index in range(count):
            preview = preview_root / f"review-{index:04d}.webp"
            if index < 4:
                Image.new("RGB", (18, 12), (index * 40, 50, 90)).save(preview, "WEBP")
            preview_sha = (
                hashlib.sha256(preview.read_bytes()).hexdigest() if preview.exists() else ""
            )
            captured = f"2024-{(index % 12) + 1:02d}-15T10:30:00"
            original = workspace.root.parent / "synthetic-source" / f"item-{index:04d}.jpg"
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,
                    captured_at,capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id,preview_path,preview_version
                ) VALUES (?,?,?,?,?,?,'IMAGE','.jpg',10,20,1,?,'SYNTHETIC','HIGH','UNKNOWN',
                          'now','scan',?,'preview-v1')
                """,
                (
                    str(original),
                    str(original).casefold(),
                    str(original.parent),
                    str(original.parent).casefold(),
                    str(original.parent).casefold(),
                    original.stem,
                    captured,
                    str(preview),
                ),
            )
            media_id = int(cursor.lastrowid)
            ids.append(media_id)
            connection.execute(
                """
                INSERT INTO media_preprocess(
                    media_id,source_fingerprint,preview_fingerprint,preview_path,
                    preview_version,preview_status,perceptual_hash,perceptual_hash_version,
                    quality_json,quality_score,preview_width,preview_height,updated_at,preview_sha256
                ) VALUES (?,?,?,?,?,'READY',?,'dhash64-v1',?,0.7,18,12,'now',?)
                """,
                (
                    media_id,
                    f"source-{index}",
                    f"preview-{index}",
                    str(preview),
                    "preview-v1",
                    f"{index:016x}"[-16:],
                    json.dumps(
                        {
                            "sharpness": 0.7,
                            "exposure": 0.6,
                            "clipping": 0.1,
                            "resolution": 216,
                            "score": 0.7,
                            "advisory": "KEEP",
                        }
                    ),
                    preview_sha,
                ),
            )
            category = "01_家庭生活" if index % 2 == 0 else "02_旅行风光"
            disposition = "REJECT_CANDIDATE" if index == 0 else "REVIEW"
            connection.execute(
                """
                INSERT INTO ai_analysis(
                    media_id,input_fingerprint,preview_fingerprint,preview_version,
                    provider,model,prompt_version,schema_version,scene_category,disposition,
                    confidence,quality_score,tags_json,short_desc,reason,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,0.9,0.7,?,?,?,'2024-01-01T00:00:00')
                """,
                (
                    media_id,
                    f"input-{index}",
                    f"preview-{index}",
                    "preview-v1",
                    "fake",
                    "fake-v1",
                    "prompt-v1",
                    "schema-v1",
                    category,
                    disposition,
                    json.dumps(["synthetic", f"tag-{index}"]),
                    f"description {index}",
                    f"reason {index}",
                ),
            )
        connection.commit()
    return tuple(ids)


def seed_groups(workspace: Workspace, media_ids: tuple[int, ...]) -> None:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO duplicate_group VALUES ('dup-stable',?,'abc',10,'now')",
            (DUPLICATE_ALGORITHM_VERSION,),
        )
        connection.executemany(
            "INSERT INTO duplicate_member(group_id,media_id) VALUES ('dup-stable',?)",
            [(media_ids[2],), (media_ids[0],)],
        )
        connection.execute(
            """
            INSERT INTO burst_group(
                id,algorithm_version,representative_media_id,time_window_seconds,
                distance_threshold,created_at,comparison_cap
            ) VALUES ('burst-stable',?,?,3.0,8,'now',32)
            """,
            (BURST_ALGORITHM_VERSION, media_ids[1]),
        )
        connection.executemany(
            """
            INSERT INTO burst_member(
                group_id,media_id,distance,quality_score,is_representative,is_best_shot
            ) VALUES ('burst-stable',?,?,0.7,?,?)
            """,
            [
                (media_ids[2], 2, 0, 0),
                (media_ids[1], 0, 1, 1),
            ],
        )
        connection.commit()


@contextmanager
def running_server(workspace: Workspace):  # type: ignore[no-untyped-def]
    server = create_review_server(workspace, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def post_decision(
    server,  # type: ignore[no-untyped-def]
    base: str,
    media_id: int,
    payload: dict[str, object],
    *,
    token: str | None = None,
):  # type: ignore[no-untyped-def]
    request = Request(
        f"{base}/api/items/{media_id}/decision",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": base,
            "X-SPT-CSRF": token if token is not None else server.csrf_token,
        },
    )
    return urlopen(request, timeout=5)


def test_t_e_001_server_is_loopback_only(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    with pytest.raises(ValueError, match="loopback"):
        create_review_server(workspace, host="0.0.0.0")

    server = create_review_server(workspace, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_t_e_002_ui_assets_have_no_cdn_or_external_url(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    with running_server(workspace) as (_server, base):
        bodies = []
        for path in ("/", "/assets/app.css", "/assets/app.js"):
            with urlopen(base + path, timeout=5) as response:
                bodies.append(response.read().decode())

    combined = "\n".join(bodies).lower()
    assert "https://" not in combined
    assert "http://" not in combined
    assert "//cdn" not in combined


def test_t_e_003_offline_ui_serves_existing_thumbnail_locally(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]

    with (
        running_server(workspace) as (_server, base),
        urlopen(f"{base}/api/preview/{media_id}", timeout=5) as response,
    ):
        body = response.read()
        assert response.headers["Content-Type"] == "image/webp"

    assert body.startswith(b"RIFF") and b"WEBP" in body[:16]


def test_t_e_004_human_keep_overrides_ai_reject(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    store = ReviewStore(workspace)

    updated = store.update_decision(
        media_id,
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )

    assert updated["effective_disposition"] == "KEEP"
    assert updated["decision_source"] == "HUMAN"
    assert updated["human_overrides_ai"] is True


def test_t_e_005_ai_rerun_does_not_erase_human_decision(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    ReviewStore(workspace).update_decision(
        media_id,
        category="03_工作与文档",
        disposition="KEEP",
        expected_revision=0,
    )
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ai_analysis(
                media_id,input_fingerprint,preview_fingerprint,preview_version,
                provider,model,prompt_version,schema_version,scene_category,disposition,
                confidence,quality_score,tags_json,short_desc,reason,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,0.99,0.8,'[]','new','new reason',
                      '2025-01-01T00:00:00')
            """,
            (
                media_id,
                "new-input",
                "preview-0",
                "preview-v1",
                "fake",
                "fake-v2",
                "prompt-v2",
                "schema-v1",
                "05_其他",
                "REJECT_CANDIDATE",
            ),
        )
        connection.commit()

    item = ReviewStore(workspace).get_item(media_id)

    assert item is not None
    assert item["effective_category"] == "03_工作与文档"
    assert item["effective_disposition"] == "KEEP"
    assert item["decision_source"] == "HUMAN"
    assert item["ai_disposition"] == "REJECT_CANDIDATE"


def test_effective_priority_is_human_then_latest_ai_then_rule(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed_review_items(workspace, 2)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("DELETE FROM ai_analysis WHERE media_id = ?", (media_ids[1],))
        connection.execute(
            """
            INSERT INTO ai_analysis(
                media_id,input_fingerprint,preview_fingerprint,preview_version,
                provider,model,prompt_version,schema_version,scene_category,disposition,
                confidence,quality_score,tags_json,short_desc,reason,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,0.95,0.8,'[]','tie winner','latest id wins',
                      '2024-01-01T00:00:00')
            """,
            (
                media_ids[0],
                "tie-input",
                "preview-0",
                "preview-v1",
                "fake",
                "fake-tie",
                "prompt-tie",
                "schema-v1",
                "02_旅行风光",
                "KEEP",
            ),
        )
        connection.commit()

    ai_item = ReviewStore(workspace).get_item(media_ids[0])
    rule_item = ReviewStore(workspace).get_item(media_ids[1])

    assert ai_item is not None and rule_item is not None
    assert ai_item["effective_category"] == "02_旅行风光"
    assert ai_item["effective_disposition"] == "KEEP"
    assert ai_item["decision_source"] == "AI"
    assert rule_item["effective_category"] == "05_其他"
    assert rule_item["effective_disposition"] == "REVIEW"
    assert rule_item["decision_source"] == "RULE"


def test_t_e_006_007_category_and_disposition_persist_after_reload(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    first = ReviewStore(workspace).update_decision(
        media_id,
        category="04_截图与备忘",
        disposition="REVIEW",
        expected_revision=0,
    )

    reloaded = ReviewStore(workspace).get_item(media_id)

    assert first["revision"] == 1
    assert reloaded is not None
    assert reloaded["effective_category"] == "04_截图与备忘"
    assert reloaded["effective_disposition"] == "REVIEW"
    assert reloaded["revision"] == 1

    second = ReviewStore(workspace).update_decision(
        media_id,
        category="02_旅行风光",
        disposition="REJECT_CANDIDATE",
        expected_revision=1,
    )
    assert second["effective_category"] == "02_旅行风光"
    assert second["effective_disposition"] == "REJECT_CANDIDATE"
    assert second["revision"] == 2


def test_t_e_008_exact_and_burst_group_views_are_stable(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed_review_items(workspace, 4)
    seed_groups(workspace, media_ids)
    store = ReviewStore(workspace)

    duplicate = store.list_items(duplicate_group="dup-stable", page_size=100)
    burst = store.list_items(burst_group="burst-stable", page_size=100)

    assert [item["id"] for item in duplicate.items] == [media_ids[0], media_ids[2]]
    assert [item["id"] for item in burst.items] == [media_ids[1], media_ids[2]]
    best = next(item for item in burst.items if item["id"] == media_ids[1])
    assert best["is_best_shot"] is True
    assert best["burst_group"] == "burst-stable"


def test_t_e_009_api_paginates_1000_items_and_returns_only_current_page(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_review_items(workspace, 1005)

    with running_server(workspace) as (_server, base):
        payload = get_json(f"{base}/api/items?page=2&page_size=100")

    assert payload["total"] == 1005
    assert payload["page"] == 2
    assert len(payload["items"]) == 100


def test_review_filters_year_month_category_disposition_and_search(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_review_items(workspace, 24)
    store = ReviewStore(workspace)

    result = store.list_items(
        year=2024,
        month=1,
        category="01_家庭生活",
        disposition="REJECT_CANDIDATE",
        query="description 0",
    )

    assert result.total == 1
    assert result.items[0]["filename"] == "item-0000.jpg"
    assert store.list_items(query="synthetic-source").total == 0
    assert store.list_items(query="item-0000.jpg").total == 1


def test_write_api_requires_csrf_and_rejects_stale_revision(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    payload = {
        "category": "01_家庭生活",
        "disposition": "KEEP",
        "expected_revision": 0,
    }

    with running_server(workspace) as (server, base):
        status, _body = raw_http(
            server,
            "POST",
            f"/api/items/{media_id}/decision",
            headers={
                "Origin": base,
                "X-SPT-CSRF": "bad-token",
                "Content-Type": "application/json",
            },
        )
        assert status == 403
        with post_decision(server, base, media_id, payload) as response:
            assert json.loads(response.read())["revision"] == 1
        with pytest.raises(HTTPError) as stale:
            post_decision(server, base, media_id, payload)
        assert stale.value.code == 409


def test_store_rejects_invalid_values_and_optimistic_conflict(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    store = ReviewStore(workspace)

    with pytest.raises(ReviewError, match="category"):
        store.update_decision(
            media_id,
            category="../../escape",
            disposition="KEEP",
            expected_revision=0,
        )
    store.update_decision(
        media_id,
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )
    with pytest.raises(ReviewConflictError):
        store.update_decision(
            media_id,
            category="01_家庭生活",
            disposition="REVIEW",
            expected_revision=0,
        )


def test_human_write_does_not_mutate_ai_rows(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        before = connection.execute(
            "SELECT * FROM ai_analysis WHERE media_id = ?", (media_id,)
        ).fetchall()

    ReviewStore(workspace).update_decision(
        media_id,
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        after = connection.execute(
            "SELECT * FROM ai_analysis WHERE media_id = ?", (media_id,)
        ).fetchall()
        human = connection.execute(
            "SELECT decision_source,revision FROM review_decision WHERE media_id = ?",
            (media_id,),
        ).fetchone()
    assert after == before
    assert human == ("HUMAN", 1)


def test_preview_endpoint_refuses_database_path_outside_workspace(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("DO_NOT_SERVE", encoding="utf-8")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET preview_path = ? WHERE media_id = ?",
            (str(outside), media_id),
        )
        connection.commit()

    with pytest.raises(ReviewError, match="controlled previews"):
        ReviewStore(workspace).read_preview(media_id)

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET preview_path = ? WHERE media_id = ?",
            (str(workspace.root / "previews"), media_id),
        )
        connection.commit()
    with pytest.raises(ReviewError, match="controlled previews"):
        ReviewStore(workspace).read_preview(media_id)


def test_preview_endpoint_refuses_changed_artifact_digest(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    preview = workspace.root / "previews" / "review-0000.webp"
    preview.write_bytes(b"changed-after-ready")

    with pytest.raises(ReviewError, match="identity changed"):
        ReviewStore(workspace).read_preview(media_id)


def test_ui_uses_text_nodes_and_never_inner_html_for_database_fields(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    with (
        running_server(workspace) as (_server, base),
        urlopen(f"{base}/assets/app.js", timeout=5) as response,
    ):
        javascript = response.read().decode()

    assert "textContent" in javascript
    assert "innerHTML" not in javascript


def test_v9_upgrades_additively_to_v10_and_preserves_ai(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "upgrade.sqlite3"
    migrations_v9 = tuple(migration for migration in MIGRATIONS if migration.version <= 9)
    migrations_v10 = tuple(migration for migration in MIGRATIONS if migration.version <= 10)
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations_v9)
    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection) == 9
        connection.execute(
            """
            INSERT INTO ai_run(
                id,provider,model,prompt_version,schema_version,started_at,status
            ) VALUES ('preserved','fake','fake-v1','p1','s1','now','COMPLETE')
            """
        )
        connection.commit()

    monkeypatch.setattr(database_module, "MIGRATIONS", migrations_v10)
    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection) == 10
        assert connection.execute(
            "SELECT provider,status FROM ai_run WHERE id='preserved'"
        ).fetchone() == ("fake", "COMPLETE")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='review_decision'"
        ).fetchone() == ("review_decision",)


def test_review_cli_contract_is_registered() -> None:
    args = build_parser().parse_args(["review", "--no-open", "--port", "0"])

    assert args.command == "review"
    assert args.host == "127.0.0.1"
    assert args.no_open is True


def test_http_api_supports_prd_filters_without_unbounded_results(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_review_items(workspace, 240)
    query = urlencode(
        {
            "page": 1,
            "page_size": 50,
            "year": 2024,
            "month": 1,
            "category": "01_家庭生活",
            "disposition": "REJECT_CANDIDATE",
            "q": "description",
        }
    )

    with running_server(workspace) as (_server, base):
        payload = get_json(f"{base}/api/items?{query}")

    assert len(payload["items"]) <= 50
    assert payload["page_size"] == 50


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"page": 0}, "page"),
        ({"page_size": 101}, "page_size"),
        ({"year": 0}, "year"),
        ({"month": 13}, "month"),
        ({"category": "unknown"}, "category"),
        ({"disposition": "DELETE"}, "disposition"),
        ({"query": "x" * 201}, "search"),
        ({"duplicate_group": ""}, "duplicate"),
        ({"burst_group": "x" * 201}, "burst"),
    ],
)
def test_review_store_rejects_bounded_query_inputs(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    with pytest.raises(ReviewError, match=message):
        ReviewStore(workspace).list_items(**arguments)


def test_review_store_rejects_invalid_ids_missing_items_and_values(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = ReviewStore(workspace)

    with pytest.raises(ReviewError, match="media id"):
        store.get_item(0)
    with pytest.raises(ReviewError, match="media id"):
        store.update_decision(0, category="01_家庭生活", disposition="KEEP", expected_revision=0)
    with pytest.raises(ReviewError, match="disposition"):
        store.update_decision(1, category="01_家庭生活", disposition="DELETE", expected_revision=0)
    with pytest.raises(ReviewError, match="revision"):
        store.update_decision(1, category="01_家庭生活", disposition="KEEP", expected_revision=True)
    with pytest.raises(ReviewError, match="not found"):
        store.update_decision(999, category="01_家庭生活", disposition="KEEP", expected_revision=0)
    with pytest.raises(ReviewError, match="media id"):
        store.read_preview(0)
    with pytest.raises(ReviewError, match="not found"):
        store.read_preview(999)


def test_malformed_advisory_json_is_rejected_as_local_state_error(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE ai_analysis SET tags_json = 'not-json' WHERE media_id = ?", (media_id,)
        )
        connection.execute(
            "UPDATE media_preprocess SET quality_json = '[]' WHERE media_id = ?", (media_id,)
        )
        connection.commit()

    with pytest.raises(ReviewError, match="LOCAL_STATE_ERROR"):
        ReviewStore(workspace).get_item(media_id)


def test_preview_missing_file_is_redacted_and_unknown_suffix_is_bounded(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    preview = workspace.root / "previews" / "review-0000.webp"
    unknown = preview.with_suffix(".bin")
    preview.rename(unknown)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET preview_path = ? WHERE media_id = ?",
            (str(unknown), media_id),
        )
        connection.commit()

    content_type, body = ReviewStore(workspace).read_preview(media_id)
    assert content_type == "image/webp"
    assert body.startswith(b"RIFF")
    unknown.unlink()
    with pytest.raises(ReviewError, match="safely"):
        ReviewStore(workspace).read_preview(media_id)


def raw_http(
    server,  # type: ignore[no-untyped-def]
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload
    finally:
        connection.close()


def test_http_rejects_host_malformed_queries_and_unavailable_routes(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    with running_server(workspace) as (server, _base):
        status, payload = raw_http(server, "GET", "/", headers={"Host": "evil.invalid"})
        assert (status, payload["error"]) == (400, "INVALID_HOST")
        status, bootstrap = raw_http(server, "GET", "/api/bootstrap")
        assert status == 200
        assert bootstrap["page_size"] == 100
        for path, expected in (
            ("/api/items?bad", 400),
            ("/api/items?page=nope", 400),
            ("/api/items?page=1&page=2", 400),
            ("/api/preview/999", 404),
            ("/missing", 404),
        ):
            status, _payload = raw_http(server, "GET", path)
            assert status == expected


def test_http_rejects_all_unsafe_write_shapes(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]

    with running_server(workspace) as (server, base):
        good_headers = {
            "Origin": base,
            "X-SPT-CSRF": server.csrf_token,
            "Content-Type": "application/json",
        }
        status, _payload = raw_http(
            server,
            "POST",
            f"/api/items/{media_id}/decision",
            headers={**good_headers, "Host": "evil.invalid"},
        )
        assert status == 400
        cases = (
            ("/api/items/1/decision?x=1", None, good_headers, 404),
            (
                f"/api/items/{media_id}/decision",
                None,
                {**good_headers, "Origin": "http://evil.invalid"},
                403,
            ),
            (
                f"/api/items/{media_id}/decision",
                None,
                {**good_headers, "Content-Type": "text/plain"},
                415,
            ),
            (f"/api/items/{media_id}/decision", b"{", good_headers, 400),
            (f"/api/items/{media_id}/decision", b"{}", good_headers, 400),
            (
                f"/api/items/{media_id}/decision",
                json.dumps(
                    {
                        "category": "01_家庭生活",
                        "disposition": "DELETE",
                        "expected_revision": 0,
                    }
                ).encode(),
                good_headers,
                400,
            ),
        )
        for path, body, headers, expected in cases:
            status, _payload = raw_http(server, "POST", path, body=body, headers=headers)
            assert status == expected

        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", f"/api/items/{media_id}/decision")
            connection.putheader("Origin", base)
            connection.putheader("X-SPT-CSRF", server.csrf_token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "4097")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 413
            response.read()
        finally:
            connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", f"/api/items/{media_id}/decision")
            connection.putheader("Origin", base)
            connection.putheader("X-SPT-CSRF", server.csrf_token)
            connection.putheader("Content-Type", "application/json")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 411
            response.read()
        finally:
            connection.close()


def test_http_redacts_local_database_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]

    with running_server(workspace) as (server, base):

        def database_failure(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            raise sqlite3.OperationalError("SECRET_LOCAL_PATH")

        monkeypatch.setattr(server.store, "list_items", database_failure)
        status, payload = raw_http(server, "GET", "/api/items")
        assert (status, payload) == (500, {"error": "LOCAL_STATE_ERROR"})
        monkeypatch.setattr(server.store, "read_preview", database_failure)
        status, payload = raw_http(server, "GET", f"/api/preview/{media_id}")
        assert (status, payload) == (500, {"error": "LOCAL_STATE_ERROR"})
        monkeypatch.setattr(server.store, "update_decision", database_failure)
        status, payload = raw_http(
            server,
            "POST",
            f"/api/items/{media_id}/decision",
            body=json.dumps(
                {
                    "category": "01_家庭生活",
                    "disposition": "KEEP",
                    "expected_revision": 0,
                }
            ).encode(),
            headers={
                "Origin": base,
                "X-SPT-CSRF": server.csrf_token,
                "Content-Type": "application/json",
            },
        )
        assert (status, payload) == (500, {"error": "LOCAL_STATE_ERROR"})


def test_create_server_and_serve_review_lifecycle_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    with pytest.raises(ValueError, match="numeric loopback"):
        create_review_server(workspace, host="localhost")
    with pytest.raises(ValueError, match="port"):
        create_review_server(workspace, port=True)

    class InterruptingServer:
        server_address = ("127.0.0.1", 43210)

        def __init__(self) -> None:
            self.closed = False

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    fake = InterruptingServer()
    opened: list[str] = []
    monkeypatch.setattr(review_module, "create_review_server", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(review_module.webbrowser, "open", lambda url, new=0: opened.append(url))

    review_module.serve_review(workspace, open_browser=True)

    assert opened == ["http://127.0.0.1:43210/"]
    assert fake.closed is True

    fake.closed = False
    monkeypatch.setattr(
        review_module.webbrowser,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("browser failed")),
    )
    with pytest.raises(OSError, match="browser failed"):
        review_module.serve_review(workspace, open_browser=True)
    assert fake.closed is True


def test_review_cli_invokes_injectable_local_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(
        cli_module,
        "serve_review",
        lambda _workspace, *, host, port, open_browser: calls.append((host, port, open_browser)),
    )

    result = main(
        ["review", "--workspace", str(tmp_path / "workspace"), "--port", "0", "--no-open"]
    )

    assert result == 0
    assert calls == [("127.0.0.1", 0, False)]


def test_review_groups_ignore_old_algorithm_membership_and_flags(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed_review_items(workspace, 3)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executemany(
            "INSERT INTO duplicate_group VALUES (?,?,?,?,?)",
            (
                ("000-old-duplicate", "exact-old-v0", "old-hash", 10, "old"),
                (
                    "zzz-current-duplicate",
                    DUPLICATE_ALGORITHM_VERSION,
                    "current-hash",
                    10,
                    "current",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO duplicate_member VALUES ('000-old-duplicate',?)", (media_ids[0],)
        )
        connection.executemany(
            "INSERT INTO duplicate_member VALUES ('zzz-current-duplicate',?)",
            ((media_ids[0],), (media_ids[1],)),
        )
        connection.executemany(
            """
            INSERT INTO burst_group(
                id,algorithm_version,representative_media_id,time_window_seconds,
                distance_threshold,created_at,comparison_cap
            ) VALUES (?,?,?,3.0,8,?,32)
            """,
            (
                ("000-old-burst", "burst-old-v0", media_ids[0], "old"),
                (
                    "zzz-current-burst",
                    BURST_ALGORITHM_VERSION,
                    media_ids[1],
                    "current",
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO burst_member VALUES ('000-old-burst',?,0,0.1,1,1)
            """,
            (media_ids[0],),
        )
        connection.executemany(
            """
            INSERT INTO burst_member VALUES ('zzz-current-burst',?,?,0.8,?,?)
            """,
            (
                (media_ids[0], 1, 0, 0),
                (media_ids[1], 0, 1, 1),
            ),
        )
        connection.commit()

    store = ReviewStore(workspace)
    duplicate = store.list_items(duplicate_group="zzz-current-duplicate")
    burst = store.list_items(burst_group="zzz-current-burst")
    first = store.get_item(media_ids[0])
    second = store.get_item(media_ids[1])

    assert [item["id"] for item in duplicate.items] == [media_ids[0], media_ids[1]]
    assert [item["id"] for item in burst.items] == [media_ids[0], media_ids[1]]
    assert first is not None and second is not None
    assert first["duplicate_group"] == "zzz-current-duplicate"
    assert first["burst_group"] == "zzz-current-burst"
    assert first["is_representative"] is False
    assert first["is_best_shot"] is False
    assert second["is_representative"] is True
    assert second["is_best_shot"] is True


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE review_decision SET revision = 'oops' WHERE media_id = ?", (1,)),
        ("UPDATE ai_analysis SET confidence = 'oops' WHERE media_id = ?", (1,)),
        ("UPDATE ai_analysis SET tags_json = 'not-json' WHERE media_id = ?", (1,)),
        ("UPDATE media_preprocess SET quality_json = 'not-json' WHERE media_id = ?", (1,)),
    ],
)
def test_malformed_database_rows_are_fixed_local_state_errors(
    tmp_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    ReviewStore(workspace).update_decision(
        media_id,
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(statement, parameters)
        connection.commit()

    with pytest.raises(ReviewError, match="LOCAL_STATE_ERROR"):
        ReviewStore(workspace).get_item(media_id)

    with running_server(workspace) as (server, _base):
        status, payload = raw_http(server, "GET", "/api/items")
    assert (status, payload) == (500, {"error": "LOCAL_STATE_ERROR"})


def test_post_with_malformed_persisted_revision_returns_fixed_json_500(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_id = seed_review_items(workspace, 1)[0]
    ReviewStore(workspace).update_decision(
        media_id,
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE review_decision SET revision = 'oops' WHERE media_id = ?", (media_id,)
        )
        connection.commit()

    with running_server(workspace) as (server, base):
        with pytest.raises(HTTPError) as response:
            post_decision(
                server,
                base,
                media_id,
                {
                    "category": "01_家庭生活",
                    "disposition": "REJECT_CANDIDATE",
                    "expected_revision": 1,
                },
            )
        payload = json.loads(response.value.read())

    assert response.value.code == 500
    assert payload == {"error": "LOCAL_STATE_ERROR"}
