# 工件登记、失效与发布

## 数据表

| 表 | 职责 |
|---|---|
| `projects` | 项目身份和生命周期根节点 |
| `artifacts` | 不可变内容寻址工件、输入摘要、存储键和失效状态 |
| `project_publications` | 每个项目当前唯一可读的 `PanoManifest` |
| `schema_migrations` | 已执行迁移版本 |

## 事务保证

1. 同一项目、同一 `content_hash` 只登记一次；重复请求返回已有工件 ID。
2. 只有 `ACTIVE` 的 `panoManifest` 工件可以发布。
3. 发布 Revision 只能单调增加；相同 Revision 和相同工件可幂等重放，旧 Revision 被拒绝。
4. 上游摘要变化后，按 `input_digests` 传递计算所有下游工件，并在同一事务中标为 `INVALIDATED`。
5. 如果失效集合包含当前发布 Manifest，则同步删除 `project_publications`，Viewer 不会继续读旧结果。
6. 旧工件不会物理删除，仍可用于审计和复现。

## 本地验证

```powershell
docker compose up -d postgres
$env:TEST_DATABASE_URL='postgresql://pano:pano@127.0.0.1:55432/pano'
python -m pytest -q tests/test_postgres_repository.py
docker compose stop postgres
```

Compose 使用宿主端口 55432，避免与本机已有 PostgreSQL 5432 冲突；容器和 API 网络内部仍使用 `postgres:5432`。

完整 Compose 的 API 映射为 `127.0.0.1:18000 -> api:8000`，避免占用宿主已有 8000 端口。

集成用例验证：迁移幂等、工件登记幂等、Revision 1 发布、Layout 摘要传递失效、当前发布撤销、Revision 2 重新发布和旧 Revision 拒绝。
