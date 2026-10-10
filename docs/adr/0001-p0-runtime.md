# ADR-0001：P0 运行架构

- 状态：Accepted；AI Provider 部分由 ADR-0002 修订
- 日期：2026-08-24

## 决策

P0 固定为一条自托管垂直链路：

| 边界 | 选择 |
|---|---|
| Web | React + TypeScript；仅通过版本化 Command Facade 调用后端 |
| API | FastAPI 模块化单体 |
| CPU 异步任务 | Python Worker，Redis + RQ；PostgreSQL 是任务状态事实源 |
| 结构全景 | 独立 Headless Capture Worker，Three.js Cubemap → ERP |
| AI 增强 | 服务端 GPT Image 2 Adapter；ComfyUI 不作为 P0 主路径 |
| 数据 | PostgreSQL + S3 协议对象存储；工件不可变 |
| 本地编排 | Docker Compose |

## 约束

- 不保留 React/Vue、RQ/Celery、浏览器/服务端捕获等并列运行时选项。
- 不实现远程 Solver Provider，第二个真实实现出现前不抽象 Provider。
- UI 不直接持有解析器、求解器或渲染器内部对象，只调用 `parse/layout/buildScene/capture/render/queryResult`。
- `LayoutRevision` 是几何事实源；布局、相机、场景和全景都是带输入摘要的派生工件。
- Three.js 负责确定性 3D/360°主链路，GPT Image 2 负责可选 AI 精渲；失败不阻断基础材质全景。
- 不引入任何第三方闭源 Bundle、私有域名或私有 API。

## 暂缓

- 前端、Capture Worker、RQ Worker 和对象存储会在对应 TODO 进入实现时加入。
- 当前 Compose 只证明 API 与基础设施可启动，因此 P0-01 尚未完成。
