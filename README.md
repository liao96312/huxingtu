# Pano platform

户型图到 360°全景的自主实现工程。当前处于 Gate A：先冻结架构与数据契约，再进入解析、布局、捕获和 AI 实现。

## 当前可运行内容

- FastAPI 健康检查与版本化 Command Facade
- `LayoutRevision`、`ArtifactManifest`、`CommandEnvelope` JSON Schema
- `PanoManifest` 与 2 房间/8 视角 Golden Viewer
- React + Three.js 全屏 Viewer：拖拽、滚轮/双指缩放、场景条、键盘切换和分享 URL
- 多尺寸比例尺校准、像素↔毫米仿射往返和工件依赖失效
- Pydantic 几何引用和门窗区间校验
- 契约、Facade 与 API Smoke Test

尚未实现的业务命令会明确返回 `501 COMMAND_NOT_IMPLEMENTED`，不会生成假任务。

## 本地验证

```powershell
python -m pytest
uvicorn pano_platform.main:app --app-dir backend --reload
cd apps/pano-viewer
npm install
npm run dev
```

健康检查：`GET http://127.0.0.1:8000/api/health`

## 容器启动

```powershell
docker compose up --build
```

当前 Compose 启动 API、PostgreSQL 和 Redis。Capture Worker、对象存储、GPT Image 2 Adapter 与超分/QA Worker 会在对应 P0 项进入实现后加入，P0-01 在此之前保持未完成。

容器 API：`GET http://127.0.0.1:18000/api/health`。宿主端口使用 18000，避免与现有 8000 服务冲突；容器内部仍为 8000。

PostgreSQL 集成测试使用宿主端口 55432：

```powershell
docker compose up -d postgres
$env:TEST_DATABASE_URL='postgresql://pano:pano@127.0.0.1:55432/pano'
python -m pytest -q
```

## 目录

```text
apps/pano-viewer/       # React + Three.js 全景 Viewer
backend/pano_platform/  # FastAPI 模块化单体与领域契约
datasets/               # PoC 户型图、Manifest 与视觉方向样张
schemas/                # 可跨语言消费的 JSON Schema
tests/                  # 最小契约和 API 检查
docs/adr/               # 已冻结的架构决策
```
