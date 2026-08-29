# Pano platform

户型图到 360°全景的自主实现工程。当前处于通用户型识别与几何融合 Gate：Viewer、单户型3D/全景和识别原型可运行，发布准确率门尚未通过。

## 当前可运行内容

- FastAPI 健康检查与版本化 Command Facade
- `parse` 命令：Base64图片输入、CV/Tuzi模式、显式第三方授权、阶段证据持久化和结果查询
- `LayoutRevision`、`ArtifactManifest`、`CommandEnvelope` JSON Schema
- `PanoManifest` 与 fp-001 五房间结构/AI全景 Viewer
- React + Three.js 全屏 Viewer：拖拽、滚轮/双指缩放、场景条、键盘切换和分享 URL
- 多尺寸比例尺校准、像素↔毫米仿射往返和工件依赖失效
- Pydantic 几何引用和门窗区间校验
- 契约、Facade 与 API Smoke Test

`layout/buildScene/capture/render/queryResult` 等尚未实现的业务命令会明确返回 `501 COMMAND_NOT_IMPLEMENTED`。通用户型发布门当前仍为 `releaseReady=false`。

## 本地验证

```powershell
python -m pytest
uvicorn pano_platform.main:app --app-dir backend --reload
cd apps/pano-viewer
npm install
npm run dev
```

健康检查：`GET http://127.0.0.1:8000/api/health`

上传户型图端到端测试：`http://127.0.0.1:5173/?pipeline=1`

该页面会依次执行上传门禁、CV墙图、Tuzi视觉草案、几何融合、预览
`LayoutRevision/SceneGeometry`、2:1结构ERP和动态`PanoManifest`，最后给出可拖动的
360°Viewer链接。Tuzi image-2装修是显式付费选项，只处理面积最大的首个房间；失败时
自动保留结构全景。未过几何质量门的输出只标为“预览/待复核”，不会冒充正式Revision。

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
docs/plan/              # 户型识别CV+Tuzi视觉模型方案与详细TODO
```

户型识别实施基线：[CV + Tuzi视觉模型 + 几何融合方案](docs/plan/floorplan-recognition-v2.md)；执行清单见 [识别v2详细TODO](docs/plan/floorplan-recognition-v2-todo.md)。
