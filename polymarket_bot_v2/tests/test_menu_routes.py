"""
test_menu_routes.py — T302 应用菜单管理 API 测试
"""
import scripts.web_dashboard as wd
from fastapi.testclient import TestClient
from scripts.dashboard_routes import _shared


def _admin_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(_shared, "TOTP_SECRET", "")
    c = TestClient(wd.app)
    c.cookies.set("dashboard_key", "k")
    return c


def _anon_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "")
    return TestClient(wd.app)


def _ensure_menu_table():
    """确保 app_menus 表存在 (schema_version v33)。在某些测试 DB 下需要显式确保。"""
    try:
        with _shared.db_connect() as conn:
            conn.execute("SELECT 1 FROM app_menus LIMIT 1")
    except Exception:
        # 触发 db.init_db 跑完所有迁移
        from src.utils import db as _db
        _db.init_db()


def test_menu_admin_list_requires_admin(monkeypatch):
    _ensure_menu_table()
    c = _anon_client(monkeypatch)
    r = c.get("/api/menu/admin/list")
    assert r.status_code == 403


def test_menu_admin_list_returns_items(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)
    r = c.get("/api/menu/admin/list")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert isinstance(d["items"], list)


def test_menu_create_update_delete_flow(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)

    # create
    r = c.post("/api/menu/admin/create", json={
        "label": "测试菜单",
        "path": "/v2/__test_menu_t302__",
        "icon": "circle",
        "sort_order": 999,
        "role_required": "user",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # find created id
    rows = c.get("/api/menu/admin/list").json()["items"]
    target = next((m for m in rows if m["path"] == "/v2/__test_menu_t302__"), None)
    assert target is not None
    mid = target["id"]
    assert target["sort_order"] == 999
    assert target["role_required"] == "user"

    # update
    r = c.post("/api/menu/admin/update", json={"id": mid, "label": "改名后", "visible": False})
    assert r.status_code == 200 and r.json()["ok"] is True

    rows2 = c.get("/api/menu/admin/list").json()["items"]
    t2 = next(m for m in rows2 if m["id"] == mid)
    assert t2["label"] == "改名后"
    assert t2["visible"] is False

    # delete
    r = c.post("/api/menu/admin/delete", json={"id": mid})
    assert r.status_code == 200 and r.json()["ok"] is True

    rows3 = c.get("/api/menu/admin/list").json()["items"]
    assert all(m["id"] != mid for m in rows3)


def test_menu_create_validates_path(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)
    r = c.post("/api/menu/admin/create", json={"label": "x", "path": "no_slash"})
    assert r.status_code == 400
    assert "slash" in r.json()["error"].lower()


def test_menu_create_rejects_invalid_role(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)
    r = c.post("/api/menu/admin/create", json={
        "label": "x", "path": "/v2/__role_test__", "role_required": "evil"
    })
    assert r.status_code == 400


def test_menu_create_duplicate_path_returns_409(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)
    payload = {"label": "重复", "path": "/v2/__dup_test_t302__", "role_required": "user"}
    c.post("/api/menu/admin/create", json=payload)
    r = c.post("/api/menu/admin/create", json=payload)
    assert r.status_code == 409
    # cleanup
    rows = c.get("/api/menu/admin/list").json()["items"]
    target = next((m for m in rows if m["path"] == payload["path"]), None)
    if target:
        c.post("/api/menu/admin/delete", json={"id": target["id"]})


def test_menu_user_list_filters_admin_only_for_non_admin(monkeypatch):
    _ensure_menu_table()
    admin = _admin_client(monkeypatch)
    # create an admin-only menu
    admin.post("/api/menu/admin/create", json={
        "label": "仅管理员", "path": "/v2/__admin_only_t302__",
        "role_required": "admin", "visible": True,
    })

    # anon (non-admin)
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "")
    anon = TestClient(wd.app)
    r = anon.get("/api/menu/list")
    assert r.status_code == 200
    paths = [m["path"] for m in r.json()["items"]]
    assert "/v2/__admin_only_t302__" not in paths

    # cleanup
    rows = admin.get("/api/menu/admin/list").json()["items"]
    target = next((m for m in rows if m["path"] == "/v2/__admin_only_t302__"), None)
    if target:
        admin.post("/api/menu/admin/delete", json={"id": target["id"]})


def test_menu_reorder(monkeypatch):
    _ensure_menu_table()
    c = _admin_client(monkeypatch)
    rows = c.get("/api/menu/admin/list").json()["items"]
    if len(rows) < 2:
        # seed two
        c.post("/api/menu/admin/create", json={"label": "A", "path": "/v2/__reorder_a__"})
        c.post("/api/menu/admin/create", json={"label": "B", "path": "/v2/__reorder_b__"})
        rows = c.get("/api/menu/admin/list").json()["items"]
    a, b = rows[0], rows[1]
    r = c.post("/api/menu/admin/reorder", json={
        "items": [{"id": a["id"], "sort_order": 555}, {"id": b["id"], "sort_order": 666}]
    })
    assert r.status_code == 200 and r.json()["ok"] is True
