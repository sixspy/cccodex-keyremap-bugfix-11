
import hashlib, json, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingest_batches(
          batch_id TEXT PRIMARY KEY,
          old_key TEXT,
          tenant TEXT,
          declared_segments TEXT NOT NULL DEFAULT '[]',
          completeness TEXT NOT NULL DEFAULT '缺段',
          status TEXT NOT NULL DEFAULT '装载中',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_segments(
          batch_id TEXT NOT NULL,
          segment TEXT NOT NULL,
          payload TEXT NOT NULL,
          loaded_at TEXT NOT NULL,
          PRIMARY KEY(batch_id, segment)
        );
        CREATE TABLE IF NOT EXISTS parts_ledger(
          batch_id TEXT NOT NULL,
          part TEXT NOT NULL,
          present INTEGER NOT NULL,
          PRIMARY KEY(batch_id, part)
        );
        CREATE TABLE IF NOT EXISTS attest_buckets(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL UNIQUE,
          bucket_key TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS remap_receipts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL UNIQUE,
          receipt_no TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingest_rejections(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL,
          old_key TEXT,
          reason TEXT NOT NULL,
          detail TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingest_locks(
          batch_id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingest_contentions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL,
          contender TEXT NOT NULL,
          holder TEXT NOT NULL,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(formal_entities)").fetchall()}
        if "batch_id" not in cols:
            conn.execute("ALTER TABLE formal_entities ADD COLUMN batch_id TEXT")
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")
        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                  (m["new_key"], body.old_key, ver["version"], now))
        return {"entity_key": m["new_key"], "version": ver["version"]}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

# ---------------- 分段入站（修复卡甲路径：任一分段缺/空则整笔拒映） ----------------
REQUIRED_SEGMENTS = ("main", "tenant")
# 修复卡甲路径启用；乙路径当日不启用，甲乙互斥。
ACTIVE_PATH = "甲"

class BatchIn(BaseModel):
    batch_id: str
    old_key: str
    tenant: str
    declared_segments: list[str] = ["main", "tenant"]

class SegmentIn(BaseModel):
    segment: str
    payload: str = ""

class RemapBatchIn(BaseModel):
    force: bool = False
    path: str = "甲"

def _now():
    return datetime.now().isoformat(timespec="seconds")

def _declared_parts(batch_row) -> list[str]:
    declared = list(json.loads(batch_row["declared_segments"]))
    return list(dict.fromkeys([*REQUIRED_SEGMENTS, *declared]))

def _ledger_state(c, batch_id):
    """齐套核证唯一事实源：部件账。徽章不得作准。

    返回 (complete, missing_required, missing_declared, parts)。
    """
    b = c.execute("SELECT * FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
    declared = set(json.loads(b["declared_segments"]))
    parts = {r["part"]: r["present"] for r in
             c.execute("SELECT part,present FROM parts_ledger WHERE batch_id=?", (batch_id,)).fetchall()}
    missing_required = [p for p in REQUIRED_SEGMENTS if not parts.get(p)]
    missing_declared = [p for p in declared if not parts.get(p)]
    complete = not missing_required and not missing_declared
    return complete, missing_required, missing_declared, parts

def _recompute_completeness(c, batch_id):
    complete, _, _, _ = _ledger_state(c, batch_id)
    c.execute("UPDATE ingest_batches SET completeness=?, updated_at=? WHERE batch_id=?",
              ("齐全" if complete else "缺段", _now(), batch_id))
    return complete

def _record_rejection(c, batch_id, old_key, reason, detail, status="拒映"):
    c.execute("INSERT INTO ingest_rejections(batch_id,old_key,reason,detail,status,created_at) VALUES (?,?,?,?,?,?)",
              (batch_id, old_key, reason, detail, status, _now()))

def _acquire_ingest_lock(c, batch_id, owner):
    """非阻塞抢 ingest 锁；被占时只记争用并返回持有者，绝不改五处状态。"""
    try:
        c.execute("INSERT INTO ingest_locks(batch_id,owner,acquired_at) VALUES (?,?,?)", (batch_id, owner, _now()))
        return None
    except sqlite3.IntegrityError:
        held = c.execute("SELECT owner FROM ingest_locks WHERE batch_id=?", (batch_id,)).fetchone()
        holder = held["owner"] if held else "unknown"
        c.execute("INSERT INTO ingest_contentions(batch_id,contender,holder,reason,created_at) VALUES (?,?,?,?,?)",
                  (batch_id, owner, holder, "ingest 锁被占，后到只记争用", _now()))
        return holder

def _release_ingest_lock(c, batch_id, owner):
    c.execute("DELETE FROM ingest_locks WHERE batch_id=? AND owner=?", (batch_id, owner))

def _assert_five_way(c, batch_id):
    """五处同态校验；不同态即抛错，整笔回滚。"""
    complete, missing_required, missing_declared, parts = _ledger_state(c, batch_id)
    formal_n = c.execute("SELECT COUNT(*) n FROM formal_entities WHERE batch_id=?", (batch_id,)).fetchone()["n"]
    attest_n = c.execute("SELECT COUNT(*) n FROM attest_buckets WHERE batch_id=?", (batch_id,)).fetchone()["n"]
    receipt_n = c.execute("SELECT COUNT(*) n FROM remap_receipts WHERE batch_id=?", (batch_id,)).fetchone()["n"]
    b = c.execute("SELECT status,completeness FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not complete:
        raise HTTPException(500, f"五处不同态：部件账缺段 {missing_required or missing_declared}")
    if formal_n != 1 or attest_n != 1 or receipt_n != 1:
        raise HTTPException(500, f"五处不同态：正式/互证/回执数量 {formal_n}/{attest_n}/{receipt_n}")
    if b["status"] != "已生效" or b["completeness"] != "齐全":
        raise HTTPException(500, f"五处不同态：批次状态 {b['status']}/{b['completeness']}")

@app.post("/api/ingest/batches")
def create_batch(body: BatchIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        c.execute("INSERT INTO ingest_batches(batch_id,old_key,tenant,declared_segments,completeness,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (body.batch_id, body.old_key, body.tenant, json.dumps(body.declared_segments), "缺段", "装载中", _now(), _now()))
        for part in _declared_parts(c.execute("SELECT * FROM ingest_batches WHERE batch_id=?", (body.batch_id,)).fetchone()):
            c.execute("INSERT OR REPLACE INTO parts_ledger(batch_id,part,present) VALUES (?,?,0)", (body.batch_id, part))
        return {"batch_id": body.batch_id, "declared_segments": body.declared_segments,
                "required_segments": list(REQUIRED_SEGMENTS)}

@app.post("/api/ingest/batches/{batch_id}/segments")
def load_segment(batch_id: str, body: SegmentIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    owner = f"segment:{user}"
    result: dict | None = None
    error: HTTPException | None = None
    with connect() as c:
        c.execute("PRAGMA busy_timeout=3000")
        b = c.execute("SELECT * FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not b: raise HTTPException(404, "批次不存在")
        holder = _acquire_ingest_lock(c, batch_id, owner)
        if holder:
            # 争用记录随事务提交；五处状态一概不动
            error = HTTPException(423, f"ingest 锁被 {holder} 占用，已记争用")
        else:
            try:
                if body.segment not in _declared_parts(b):
                    error = HTTPException(422, f"分段 {body.segment} 不属本批次声明")
                else:
                    c.execute("INSERT OR REPLACE INTO batch_segments(batch_id,segment,payload,loaded_at) VALUES (?,?,?,?)",
                              (batch_id, body.segment, body.payload, _now()))
                    # 空载荷视为该段未到，不记入部件账
                    present = 1 if body.payload != "" else 0
                    c.execute("INSERT OR REPLACE INTO parts_ledger(batch_id,part,present) VALUES (?,?,?)",
                              (batch_id, body.segment, present))
                    complete = _recompute_completeness(c, batch_id)
                    result = {"batch_id": batch_id, "segment": body.segment,
                              "present": present, "completeness": "齐全" if complete else "缺段"}
            finally:
                _release_ingest_lock(c, batch_id, owner)
    if error is not None:
        raise error
    return result

@app.post("/api/ingest/batches/{batch_id}/remap")
def remap_batch(batch_id: str, body: RemapBatchIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    owner = f"remap:{user}"
    result: dict | None = None
    error: HTTPException | None = None
    with connect() as c:
        c.execute("PRAGMA busy_timeout=3000")
        b = c.execute("SELECT * FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not b: raise HTTPException(404, "批次不存在")
        holder = _acquire_ingest_lock(c, batch_id, owner)
        if holder:
            # 争用记录随事务提交；五处状态一概不动
            error = HTTPException(423, f"ingest 锁被 {holder} 占用，已记争用")
        else:
            try:
                complete, missing_required, missing_declared, _parts = _ledger_state(c, batch_id)
                badge_complete = (b["completeness"] == "齐全")
                # 偏离修复卡：走乙路径或 force 强推 —— 无论批次状态一律拒写并记「缺段偏了」
                if body.path != ACTIVE_PATH or body.force:
                    why = "path=乙（当日不启用）" if body.path != ACTIVE_PATH else "force 强推"
                    _record_rejection(c, batch_id, b["old_key"], "缺段偏离", why, status="缺段偏了")
                    error = HTTPException(409, f"偏离缺段修复卡：{why}；拒写并记缺段偏了")
                elif b["status"] == "已生效":
                    # 已生效批次重放：原样回执，不新增五处任何一行
                    rc = c.execute("SELECT receipt_no,entity_key,version FROM remap_receipts WHERE batch_id=?", (batch_id,)).fetchone()
                    if rc:
                        result = {"batch_id": batch_id, "entity_key": rc["entity_key"],
                                  "version": rc["version"], "replayed": True}
                    else:
                        error = HTTPException(409, "批次已生效但缺重映回执，五处不同态")
                elif badge_complete != complete:
                    _record_rejection(c, batch_id, b["old_key"], "缺段偏离", "徽章与部件账不符", status="缺段偏了")
                    error = HTTPException(409, "偏离缺段修复卡：徽章与部件账不符；拒写并记缺段偏了")
                elif not complete:
                    detail = f"缺必需段 {missing_required} 缺声明段 {missing_declared}"
                    _record_rejection(c, batch_id, b["old_key"], "缺段", detail, status="拒映")
                    error = HTTPException(409, "缺段，整笔拒映，正式区零增")
                else:
                    ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
                    m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], b["old_key"])).fetchone() if ver else None
                    if not ver:
                        error = HTTPException(404, "无生效映射版本")
                    elif not m:
                        c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (b["old_key"], "无映射", "挂起"))
                        _record_rejection(c, batch_id, b["old_key"], "无映射", "旧键无生效映射", status="拒映")
                        error = HTTPException(409, "无映射，已进冲突队列")
                    else:
                        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at,batch_id) VALUES (?,?,?,?,?)",
                                  (m["new_key"], b["old_key"], ver["version"], _now(), batch_id))
                        c.execute("INSERT INTO attest_buckets(batch_id,bucket_key,state,created_at) VALUES (?,?,?,?)",
                                  (batch_id, m["new_key"], "已互证", _now()))
                        c.execute("INSERT INTO remap_receipts(batch_id,receipt_no,entity_key,version,created_at) VALUES (?,?,?,?,?)",
                                  (batch_id, "R-" + batch_id, m["new_key"], ver["version"], _now()))
                        c.execute("UPDATE ingest_batches SET status='已生效', completeness='齐全', updated_at=? WHERE batch_id=?",
                                  (_now(), batch_id))
                        try:
                            _assert_five_way(c, batch_id)
                        except HTTPException as e:
                            c.rollback()
                            error = e
                        else:
                            result = {"batch_id": batch_id, "entity_key": m["new_key"], "version": ver["version"]}
            finally:
                _release_ingest_lock(c, batch_id, owner)
    if error is not None:
        raise error
    return result

@app.get("/api/ingest/batches/{batch_id}")
def get_batch(batch_id: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        b = c.execute("SELECT * FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not b: raise HTTPException(404, "批次不存在")
        parts = [dict(r) for r in c.execute("SELECT * FROM parts_ledger WHERE batch_id=? ORDER BY part", (batch_id,)).fetchall()]
        formal = [dict(r) for r in c.execute("SELECT * FROM formal_entities WHERE batch_id=?", (batch_id,)).fetchall()]
        attest = [dict(r) for r in c.execute("SELECT * FROM attest_buckets WHERE batch_id=?", (batch_id,)).fetchall()]
        receipt = [dict(r) for r in c.execute("SELECT * FROM remap_receipts WHERE batch_id=?", (batch_id,)).fetchall()]
        complete, missing_required, missing_declared, _ = _ledger_state(c, batch_id)
        badge_complete = (b["completeness"] == "齐全")
        live_effective = (b["status"] == "已生效")
        consistent = (
            badge_complete == complete
            and (not live_effective or (len(formal) == 1 and len(attest) == 1 and len(receipt) == 1))
            and (live_effective or (len(formal) == 0 and len(attest) == 0 and len(receipt) == 0))
        )
        return {"batch": dict(b), "parts": parts, "formal": formal, "attest": attest, "receipt": receipt,
                "ledger_complete": complete,
                "missing_required": missing_required, "missing_declared": missing_declared,
                "five_way_consistent": consistent}

@app.get("/api/ingest/rejections")
def ingest_rejections(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingest_rejections ORDER BY id DESC").fetchall()]}

@app.get("/api/ingest/contentions")
def ingest_contentions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingest_contentions ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
