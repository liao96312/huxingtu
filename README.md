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

该页面提交后立即获得 `202 + taskId`，前端轮询任务状态；后台依次执行上传门禁、CV墙图、Tuzi视觉草案、几何融合、预览
`LayoutRevision/SceneGeometry`、2:1结构ERP和动态`PanoManifest`，最后给出可拖动的
360°Viewer链接。Tuzi image-2装修默认开启并逐个处理所有闭合房间；单个房间失败时
只回退该房间结构全景，不阻断其他房间。未过几何质量门的输出只标为“预览/待复核”，不会冒充正式Revision。

## 容器启动

```powershell
docker compose up --build
```

当前 Compose 启动 API、PostgreSQL 和 Redis。上传测试链在单机 API 进程内执行识别、结构ERP、可选 image-2 和本地 Real-ESRGAN；RQ/Capture/对象存储 Worker 仍是后续生产化工作。

容器 API：`GET http://127.0.0.1:18000/api/health`。宿主端口使用 18000，避免与现有 8000 服务冲突；容器内部仍为 8000。

本地超分工具（不进入 Git）首次安装：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_realesrgan.ps1
```

默认使用 `tools/realesrgan/package/realesrgan-ncnn-vulkan.exe` 和 `realesrgan-x4plus`；也可用用户级环境变量 `REALESRGAN_TOOL` 指向已有可执行文件。输出为 `6144×3072`，并同步生成 `4096×2048/2048×1024/1024×512` 代理。

PostgreSQL 集成测试使用宿主端口 55432：

```powershell
docker compose up -d postgres
$env:TEST_DATABASE_URL='postgresql://pano:pano@127.0.0.1:55432/pano'
python -m pytest -q
```

## 公共真值回归

下载固定版本并执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/fetch_resplan.ps1
python scripts/prepare_resplan_truth.py --partition test --limit 10
python scripts/run_resplan_regression.py --limit 10
```

下载器会自动探测本机 `127.0.0.1:7897`，仅对本次下载使用代理并校验固定版本的字节数和 SHA-256；随后以受限反序列化生成本项目的 PNG/真值 JSON，最后实际跑无人干预上传到 Viewer 链并输出房间/门窗数量差。派生文件只写入被 Git 忽略的 `tmp/`。数据来源和许可证见 `datasets/external/registry.json`。

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
