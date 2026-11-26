# 灰度发布台（grayroll）

**所属领域：应用开发 · 子方向：全栈开发**

全栈产品：功能开关登记 → 环境绑定 → 人群圈选 → 灰度规则 → 发布生效 → 紧急熔断 → 单点覆盖 → 变更历史 → 在线求值。

## 骨架一句话

功能开关按环境配置灰度比例与人群，发布须版本落库才能生效，紧急熔断与变更历史同源。

## H3 切口候选（写路径互斥 · 须择一）

1. **发布生效**：同事务写入正式生效配置并升版本锁旧版，或只写影子发布草案、升正时原子落正式区（正式区升正前零改）。
2. **紧急熔断**：同事务立即写正式关闭并记变更，或影子紧急草案确认后原子熔断。
3. **人群绑定**：同事务写正式人群规则，或影子绑定草案升正。

正确性口径只能走甲乙中的一种且两边不得混用。

## grayroll-12 开关键更名口径（封死，不得混用）

事故：`ads.bid_curve` 更名 `ads.bid_curve_v2` 后，旧键仍命中旧正式规则、新键下规则为空。
根因是键更名与 `flag_rules` / `releases` 等 `flag_id` 外键搬迁、求值读口切换不在同一原子口径。

- **甲路径（同票即落正）**：`POST /api/flags/rename-migrate`（兼容 `/api/flags/{id}/rename`、`/api/flag_rename/migrate` 等别名）。
  一张 `flag_rename_orders` 迁移票内，同一事务完成 `flags.key` 更新与规则、发布、覆盖、熔断、爬坡、历史等全部 `flag_id` 外键的联查搬迁；任一步失败整笔回滚，不留半截键/票。
- **乙路径（影子草案 → 原子升正）**：`POST /api/flags/rename-shadow` 只写 `path=yi,status=open` 的影子迁移票，正式 `flags.key` 与正式外键零改；`POST /api/flags/rename-shadow/{id}/promote` 才在单事务内原子换键、迁完外键并切换求值读口。
  草案期求值仍钉旧键正式区并挂 `pending_rename`；未升正新键返回 `404 + code=rename_shadow_pending`，绝不回 `200/no_official_rule` 的空规则假绿。
- **互斥是硬不变量，不是取舍项**：同一 `flag-rename:{flag_id}` 同时只许一个胜者；进程内分片门闩 + SQLite `row_locks`/`BEGIN IMMEDIATE` + 锁内 fencing 重读，保证同进程与跨进程并发都只许 `[200,409]`，败方只写一条 `contention_marks`，不得写任何正式票/键/外键账。并发双请求入口：`duel_flag_rename_migrate`（见 `tests/flag_rename_race.py`，可 `python tests/duel_flag_rename_migrate.py`）。

收束：新键可命中；旧键拒识（404）；外键全部迁完（跨表无孤儿、行数不丢）；争用败方不得写正式。

红测三集合（先见红后救绿）：`tests/flag_rename_fk_red.py`（甲·外键同事务）、`tests/flag_rename_eval_fake.py`（乙·求值假绿关）、`tests/flag_rename_race.py`（争用）。

## 一键起环境

```bash
docker compose up -d --build
```

浏览器：http://localhost:11720

- `admin` / `admin123`（管理员）
- `writer` / `writer123`（发布员）
- `viewer` / `viewer123`（只读）

## 本地开发

```powershell
cd projects/17-grayroll
$env:PYTHONPATH="backend"
python -m uvicorn app:app --app-dir backend --reload --port 8020
python -m pytest
python scripts/count_loc.py
```

## 空桩

- 远程配置推送（`remote_push`）
- 实验平台桥接（`experiment_bridge`）

接口固定返回 501，题面可点名。
