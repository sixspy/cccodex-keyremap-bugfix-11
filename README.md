# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10936 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 分段入站（缺段修复卡：甲路径）

修复卡：`docs/缺段修复卡.md`（2026-11-20 定稿，当日不改口）。

- 分段装载：`POST /api/ingest/batches` → `POST /api/ingest/batches/{batch_id}/segments`（`main` / `tenant`，空载荷视同未到）
- 齐套重映：`POST /api/ingest/batches/{batch_id}/remap`（`{"path":"甲"}`）
- 任一分段缺/空 ⇒ 整笔拒映，正式区零增，拒映落库
- 齐套核证以部件账 `parts_ledger` 为唯一事实源，不信任 `completeness` 徽章
- 五处同态：部件账 / 正式实体 / 互证桶 / 装载批次 / 重映回执，写入后即时校验，不同态整笔回滚
- 补段与重映须占 `ingest` 锁；后到记争用返回 423，五处状态不变
- 乙路径当日不启用；`path=乙` 或 `force=true` 或徽章与部件账不符 ⇒ 拒写并记「缺段偏了」
- 查询：`GET /api/ingest/batches/{batch_id}`（含 `five_way_consistent`）、`GET /api/ingest/rejections`、`GET /api/ingest/contentions`

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10936/  
健康检查：GET http://127.0.0.1:10936/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`
