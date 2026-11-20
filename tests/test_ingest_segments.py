"""缺段假齐全修复卡对应回归测（先红后改）。

修复卡：docs/缺段修复卡.md
- 甲路径：任一分段空/缺 ⇒ 整笔拒映，正式区零增。
- 乙路径当日不启用；force/path=乙 一律按偏离拒写并记「缺段偏了」。
- 补段与重映并发必须占 ingest 锁，后到只记争用，不改五处状态。
- 五处同态：parts_ledger / formal_entities / attest_buckets / remap_receipts / ingest_batches。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _db_path():
    return app_module.DB_PATH


def _auth(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _new_batch(c, h, batch_id="B-1", old_key="OLD-A1", declared=None):
    r = c.post("/api/ingest/batches", headers=h, json={
        "batch_id": batch_id,
        "old_key": old_key,
        "tenant": "T-01",
        "declared_segments": declared or ["main", "tenant"],
    })
    assert r.status_code == 200, r.text
    return r.json()


def _count(table):
    con = sqlite3.connect(_db_path())
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _five_way(batch_id):
    b = c_state(batch_id)
    return {
        "parts": {r["part"]: r["present"] for r in b["parts"]},
        "formal": len(b["formal"]),
        "attest": len(b["attest"]),
        "receipt": len(b["receipt"]),
        "batch": b["batch"],
    }


def c_state(batch_id):
    client = TestClient(app)
    h = _auth(client)
    r = client.get(f"/api/ingest/batches/{batch_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 红测 1
def test_missing_tenant_segment_never_emits_formal():
    """缺租户段却曾报齐全并进正式 —— 甲路径必须整笔拒映、正式零增。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-MISS", "OLD-A1", ["main", "tenant"])
    assert c.post("/api/ingest/batches/B-MISS/segments", headers=h,
                  json={"segment": "main", "payload": "x"}).status_code == 200

    st = _five_way("B-MISS")
    # 租户段在部件账里必须记为缺
    assert st["parts"].get("tenant") == 0
    # 不得因为只有主段就报齐全
    assert st["batch"]["completeness"] != "齐全"

    r = c.post("/api/ingest/batches/B-MISS/remap", headers=h, json={"force": False, "path": "甲"})
    assert r.status_code == 409, f"缺段必须整笔拒映，实际 {r.status_code}: {r.text}"

    st = _five_way("B-MISS")
    assert st["formal"] == 0
    assert st["attest"] == 0
    assert st["receipt"] == 0
    assert st["batch"]["status"] != "已生效"

    rej = c.get("/api/ingest/rejections", headers=h).json()["items"]
    assert any(x["batch_id"] == "B-MISS" and x["reason"] == "缺段" for x in rej)


def test_missing_tenant_segment_cannot_enter_formal_after_remap_attempt():
    """补主段后反复点重映也不得绕过缺段拒映。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-MISS2", "OLD-B2", ["main", "tenant"])
    c.post("/api/ingest/batches/B-MISS2/segments", headers=h, json={"segment": "main", "payload": "x"})
    for _ in range(3):
        assert c.post("/api/ingest/batches/B-MISS2/remap", headers=h,
                      json={"force": False, "path": "甲"}).status_code == 409
    assert _count("formal_entities") == 1  # 仅种子 NEW-100，批次未增
    assert _count("remap_receipts") == 0


# ---------------------------------------------------------------- 红测 2
def test_backfill_vs_remap_lock_contention():
    """补段与重映抢 ingest 锁：后到只记争用，五处状态不变。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-LOCK", "OLD-A1", ["main", "tenant"])
    # 主段先装好，但租户段尚缺，处于可补段状态
    c.post("/api/ingest/batches/B-LOCK/segments", headers=h, json={"segment": "main", "payload": "x"})

    # 模拟另一路已持有 ingest 锁（在途事务）
    con = sqlite3.connect(_db_path())
    try:
        con.execute("INSERT INTO ingest_locks(batch_id,owner,acquired_at) VALUES (?,?,?)",
                    ("B-LOCK", "ingest:peer", "2026-11-20T10:00:00"))
        con.commit()
    finally:
        con.close()

    before = _five_way("B-LOCK")

    r = c.post("/api/ingest/batches/B-LOCK/segments", headers=h,
               json={"segment": "tenant", "payload": "y"})
    assert r.status_code == 423, f"抢锁失败必须 423，实际 {r.status_code}: {r.text}"

    after = _five_way("B-LOCK")
    assert after["parts"] == before["parts"], "后到者不得改部件账"
    assert after["formal"] == before["formal"]
    assert after["attest"] == before["attest"]
    assert after["receipt"] == before["receipt"]

    con = sqlite3.connect(_db_path())
    try:
        rows = con.execute("SELECT * FROM ingest_contentions WHERE batch_id=?", ("B-LOCK",)).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0][3] == "ingest:peer"  # holder


# ---------------------------------------------------------------- 红测 3
def test_badge_only_complete_is_deviation():
    """只改齐全徽章/绕道乙路径 ⇒ 拒写并记「缺段偏了」，不得出正式键。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-BADGE", "OLD-B1", ["main", "tenant"])
    c.post("/api/ingest/batches/B-BADGE/segments", headers=h, json={"segment": "main", "payload": "x"})

    # 人为把徽章改成齐全（模拟只改徽章）
    con = sqlite3.connect(_db_path())
    try:
        con.execute("UPDATE ingest_batches SET completeness='齐全' WHERE batch_id=?", ("B-BADGE",))
        con.commit()
    finally:
        con.close()

    r = c.post("/api/ingest/batches/B-BADGE/remap", headers=h, json={"force": True, "path": "甲"})
    assert r.status_code == 409, f"徽章与部件账不符必须拒写，实际 {r.status_code}: {r.text}"

    # 乙路径当日不启用，也不得混用
    r2 = c.post("/api/ingest/batches/B-BADGE/remap", headers=h, json={"force": False, "path": "乙"})
    assert r2.status_code == 409

    st = _five_way("B-BADGE")
    assert st["formal"] == 0
    assert st["receipt"] == 0
    rej = c.get("/api/ingest/rejections", headers=h).json()["items"]
    assert any(x["batch_id"] == "B-BADGE" and x["status"] == "缺段偏了" for x in rej), rej


# ---------------------------------------------------------------- 正路验收
def test_complete_batch_enters_formal_five_way():
    """甲路径正路：两段齐后重映，五处同态且一致。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-OK", "OLD-B1", ["main", "tenant"])

    # 缺租户段先拒映一次
    c.post("/api/ingest/batches/B-OK/segments", headers=h, json={"segment": "main", "payload": "m"})
    assert c.post("/api/ingest/batches/B-OK/remap", headers=h, json={}).status_code == 409
    assert _count("formal_entities") == 1

    # 补齐租户段
    r = c.post("/api/ingest/batches/B-OK/segments", headers=h, json={"segment": "tenant", "payload": "t"})
    assert r.json()["completeness"] == "齐全"

    r = c.post("/api/ingest/batches/B-OK/remap", headers=h, json={"path": "甲"})
    assert r.status_code == 200, r.text
    assert r.json()["entity_key"] == "NEW-200"

    body = c_state("B-OK")
    assert body["five_way_consistent"] is True
    assert body["batch"]["status"] == "已生效"
    assert len(body["formal"]) == 1 and len(body["attest"]) == 1 and len(body["receipt"]) == 1
    assert _count("formal_entities") == 2  # 种子 + 本批


def test_empty_segment_payload_does_not_satisfy_ledger():
    """空载荷分段视同未到，不得因目录里有行就报齐全。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-EMPTY", "OLD-A1", ["main", "tenant"])
    c.post("/api/ingest/batches/B-EMPTY/segments", headers=h, json={"segment": "main", "payload": "m"})
    r = c.post("/api/ingest/batches/B-EMPTY/segments", headers=h, json={"segment": "tenant", "payload": ""})
    assert r.json()["present"] == 0
    assert r.json()["completeness"] == "缺段"
    assert c.post("/api/ingest/batches/B-EMPTY/remap", headers=h, json={}).status_code == 409


def test_replay_after_effective_does_not_duplicate():
    """已生效批次重放，五处不新增。"""
    c = TestClient(app)
    h = _auth(c)
    _new_batch(c, h, "B-REPLAY", "OLD-A1", ["main", "tenant"])
    c.post("/api/ingest/batches/B-REPLAY/segments", headers=h, json={"segment": "main", "payload": "m"})
    c.post("/api/ingest/batches/B-REPLAY/segments", headers=h, json={"segment": "tenant", "payload": "t"})
    assert c.post("/api/ingest/batches/B-REPLAY/remap", headers=h, json={}).status_code == 200
    r = c.post("/api/ingest/batches/B-REPLAY/remap", headers=h, json={})
    assert r.status_code == 200 and r.json().get("replayed") is True
    body = c_state("B-REPLAY")
    assert len(body["formal"]) == 1 and len(body["attest"]) == 1 and len(body["receipt"]) == 1
    assert body["five_way_consistent"] is True
