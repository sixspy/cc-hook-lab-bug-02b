"""红测甲（争用）：左右两个会话一齐刷新，都必须看见库里刚落地的新尝试。"""
from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="stale-log-a-")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import app.db as db

from app import seed
from app.factory import create_app
from app.services import 投递

EID = "EVT-STALE-A"
OLD_ID = "TRY-STALE-A-OLD"
NEW_ID = "TRY-STALE-A-NEW"


@pytest.fixture(autouse=True)
def _隔离数据库(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "数据目录", tmp_path)
    monkeypatch.setattr(db, "库路径", tmp_path / "stale_a.db")
    投递._流水快照["行"] = None
    投递._流水快照["指纹"] = ""
    yield
    投递._流水快照["行"] = None
    投递._流水快照["指纹"] = ""


def _insert_attempt(try_id: str, status: str, code: int, when: str) -> None:
    conn = db.连接()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO 投递尝试"
            "(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (try_id, EID, "ep-billing", "sig-stale-a", status, code, status, when),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_old_round() -> None:
    if db.库路径.exists():
        db.库路径.unlink()
    seed.初始化库()
    conn = db.连接()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO 事件"
            "(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (EID, "invoice.paid", "ep-billing", "stale-a", "普通", "投递中",
             "2026-11-27T01:00:00Z", "2026-11-27T01:01:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    _insert_attempt(OLD_ID, "失败", 500, "2026-11-27T01:01:00Z")
    投递._流水快照["行"] = None


def _login_client(app):
    client = app.test_client()
    resp = client.post("/api/登录", json={"账号": "admin", "密码": "123456"})
    assert resp.status_code == 200
    return client


def test_两个会话齐刷新不得让旧快照盖住新尝试():
    _seed_old_round()
    app = create_app()

    # 上一轮页面/列表先读过一次，旧成败因此进入进程内快照。
    primer = _login_client(app)
    first = primer.get("/api/投递")
    assert first.status_code == 200
    assert any(row["尝试号"] == OLD_ID and row["状态"] == "失败" for row in first.get_json())

    # 旧票之后，新的成功尝试已经提交入库。
    _insert_attempt(NEW_ID, "成功", 200, "2026-11-27T01:02:00Z")

    barrier = threading.Barrier(2)

    def 左右齐刷() -> list[dict]:
        barrier.wait(timeout=5)
        client = _login_client(app)
        resp = client.get("/api/投递")
        assert resp.status_code == 200
        return resp.get_json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left_future = pool.submit(左右齐刷)
        right_future = pool.submit(左右齐刷)
        left, right = left_future.result(timeout=10), right_future.result(timeout=10)

    for rows in (left, right):
        mine = [row for row in rows if row["事件号"] == EID]
        by_id = {row["尝试号"]: row for row in mine}
        assert NEW_ID in by_id, f"流水未露出库里的新行：{[row['尝试号'] for row in mine]}"
        assert by_id[NEW_ID]["状态"] == "成功"
        assert by_id[NEW_ID]["响应码"] == 200
        # 最新一条必须是新尝试，不能仍把上一轮失败排在当前位置。
        assert mine[0]["尝试号"] == NEW_ID
        assert mine[0]["状态"] == "成功"
