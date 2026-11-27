"""红测丙（对账）：过期影子只能入旧账，不能被对账认成当前成败。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="stale-log-c-")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import app.db as db

from app import seed
from app.factory import create_app
from app.services import cache, 投递, 签名目录

EID = "EVT-STALE-C"
OLD_ID = "TRY-STALE-C-OLD"
NEW_ID = "TRY-STALE-C-NEW"


@pytest.fixture(autouse=True)
def _隔离数据库(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "数据目录", tmp_path)
    monkeypatch.setattr(db, "库路径", tmp_path / "stale_c.db")
    投递._流水快照["行"] = None
    投递._流水快照["指纹"] = ""
    yield
    投递._流水快照["行"] = None
    投递._流水快照["指纹"] = ""


def _row(try_id: str, status: str, code: int, when: str) -> tuple:
    return (try_id, EID, "ep-billing", "sig-stale-c", status, code, status, when)


def _seed_shadow_and_new_truth() -> None:
    if db.库路径.exists():
        db.库路径.unlink()
    seed.初始化库()
    conn = db.连接()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO 事件"
            "(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (EID, "invoice.paid", "ep-billing", "stale-c", "普通", "投递中",
             "2026-11-27T02:00:00Z", "2026-11-27T02:01:00Z"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO 投递尝试"
            "(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _row(OLD_ID, "失败", 500, "2026-11-27T02:01:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    投递._流水快照["行"] = None
    # 第一轮读库，把旧失败冻结成进程内影子。
    old_rows = 签名目录.投递列表()
    assert any(r["尝试号"] == OLD_ID and r["状态"] == "失败" for r in old_rows)

    conn = db.连接()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO 投递尝试"
            "(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _row(NEW_ID, "成功", 200, "2026-11-27T02:02:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def test_对账识别过期影子并以库内新行为当前票():
    _seed_shadow_and_new_truth()

    rep = cache.reconcile_display()

    assert rep.get("aligned") is True
    assert rep.get("账实一致") is True
    assert rep.get("当前尝试号") == NEW_ID
    assert rep.get("当前状态") == "成功"
    assert rep.get("current_attempt_id") == NEW_ID
    assert rep.get("current_status") == "成功"
    assert rep.get("shadow_current") is False
    assert rep.get("影子是否当前") is False
    assert rep.get("archive_count") == 1

    blob = json.dumps(rep, ensure_ascii=False, default=str)
    assert NEW_ID in blob
    # 旧票可以作为影子/归档出现，但不能占据当前成败字段。
    assert rep.get("active_read") in ("cutover_book", "live_flag")
    assert rep.get("truth") == cache._OK


def test_审计接口不得把过期影子报成对齐():
    _seed_shadow_and_new_truth()
    app = create_app()
    client = app.test_client()
    login = client.post("/api/登录", json={"账号": "admin", "密码": "123456"})
    assert login.status_code == 200

    resp = client.get("/api/cache/审计")
    assert resp.status_code == 200
    body = resp.get_json()
    core = body["core"]

    assert core["aligned"] is True
    assert core["panel_pack_mismatch"] is False
    assert NEW_ID in json.dumps(body, ensure_ascii=False, default=str)
