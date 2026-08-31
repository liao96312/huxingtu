# 户型图识别 v2 详细执行 TODO

> 对应方案：[floorplan-recognition-v2.md](./floorplan-recognition-v2.md)
> 原则：一次只推进一个可验证切片；任何视觉结果都不能绕过几何与质量门。

## 0. 执行顺序

```text
T00 范围与数据 → T01 Tuzi能力 → T02 契约 → T03 Adapter
→ T04 输入/Prompt → T05 证据/缓存 → T06 F1/F2 → T07 F3/F4
→ T08 编排 → T09 评测 → T10 安全成本 → T11 Shadow → T12 Assisted
```

## 1. P0阻断项

### [进行中] T00 冻结实现范围和数据谱系

依赖：无。

交付物：

- 审计 `backend/pano_platform/floorplan/`、`datasets/source/desktop-9/`、`datasets/debug/desktop9/`、`docs/plan/` 的来源与授权；
- 把代码/Schema/小型证据与原图、密钥、模型权重分开；
- 每张图登记 `sampleId/imageSha256/sourceGrant/split`；
- 冻结训练、验证、留出测试切片；
- 更新数据Manifest与删除/保留策略。

DoD：Git中无来源不明文件；图片可由SHA定位授权；测试集与调参集互斥；未授权图片不会发送Tuzi。

测试：DatasetManifest Schema、重复Hash检测、切片互斥测试。

### [进行中] T01 实现Tuzi模型发现与能力探针

依赖：T00；Windows用户级 `TUZI_API_KEY` 已配置，执行进程需继承该环境变量。

目标文件：

```text
backend/pano_platform/vision/config.py
backend/pano_platform/vision/capabilities.py
schemas/vision-capability-profile.schema.json
tests/test_vision_capabilities.py
scripts/probe_tuzi_vision.py
```

任务：

- 配置 `baseUrl/apiKeyFile/primaryModel/reviewModel/timeout`；
- 调 `/v1/models` 验证模型存在；
- 运行小图 `input_image + json_schema` 探针；
- 解析实际模型、请求ID、usage和响应结构；
- 失败归一为模型不存在、图片不支持、结构化输出不支持；
- 探针结果持久化，不得缓存Key。

DoD：无Key不发网络请求；模型不存在不进视觉路径；日志无Key/Base64/签名URL；能力摘要可审计。

### [x] T02 冻结VLM输入/输出契约

依赖：T00。

目标文件：

```text
backend/pano_platform/vision/contracts.py
schemas/vision-parse-request.schema.json
schemas/vlm-draft.schema.json
schemas/vision-run-evidence.schema.json
tests/fixtures/vlm-draft.valid.json
tests/test_vision_contracts.py
```

任务：定义 `VisionParseRequest/VisionReviewRequest/ScaleCandidate/RoomDraft/OpeningDraft/VlmDraft/VisionRunEvidence`；拒绝JSON截断、越界、自交、未知枚举、NaN、额外字段和数量超限。

DoD：有效Fixture同时通过Pydantic和JSON Schema；无效Fixture均有明确Reason Code。

### [x] T03 实现Tuzi Chat Completions Adapter

依赖：T01、T02。

目标文件：

```text
backend/pano_platform/vision/provider.py
backend/pano_platform/vision/tuzi_responses.py
tests/test_tuzi_responses_adapter.py
```

任务：实现Provider Protocol；构造 `/v1/chat/completions`；使用严格JSON Schema；解析结构化结果；记录请求ID/实际模型/usage/延迟/响应Hash；有限重试；禁止从自由文本猜JSON。

DoD：Mock测试覆盖成功、429、5xx、超时、审核、模型不存在、截断和Schema错误；真实Smoke仅本地Key存在时运行，CI默认跳过。

### [进行中] T04 冻结视觉输入和Prompt版本

依赖：T02、T03。

目标文件：

```text
backend/pano_platform/vision/input_artifact.py
backend/pano_platform/vision/prompts/floorplan_parse_v1.txt
backend/pano_platform/vision/prompts/floorplan_review_v1.txt
tests/test_vision_input.py
```

任务：清EXIF；校验MIME/Magic Bytes/像素/字节；冻结裁白边和结构增强策略；Prompt明确原图尺寸、坐标、枚举和禁止项；Review Prompt只能评给定候选；版本进入JobKey。

DoD：同图同版本生成相同工件和JobKey；Prompt或预处理变化必定换键。

### [进行中] T05 实现证据、缓存、幂等与错误信封

依赖：T03、T04。

目标文件：

```text
backend/pano_platform/vision/cache.py
backend/pano_platform/vision/errors.py
backend/pano_platform/vision/evidence.py
tests/test_vision_cache.py
tests/test_vision_errors.py
```

任务：实现完整JobKey、规范Draft、响应Hash、旧Revision抑制、错误归一和原始响应TTL。

DoD：并发同请求只有一个有效任务；模型/Prompt/Schema变化不复用；日志不泄密。

## 2. 几何融合

### [进行中] T06 实现F1吸附与F2比例尺/面积自洽

依赖：T02；复用现有 `wall_graph.py`、`rooms.py`、`calibrate_scale()`。

目标文件：

```text
backend/pano_platform/floorplan/fusion.py
backend/pano_platform/floorplan/fusion_evidence.py
tests/test_floorplan_fusion_scale.py
tests/test_floorplan_fusion_rooms.py
```

任务：解归一；吸附WallGraph；动态墙厚容差；OCR尺寸中位数/MAD拟合；墙厚比例交叉校验；面积自洽；记录全部Evidence。

DoD：Golden Fixture达到吸附/比例/面积阈值；失败结果只进复核。

### [进行中] T07 实现F3开口仲裁与F4房间闭合

依赖：T06。

目标文件：

```text
backend/pano_platform/floorplan/fusion_openings.py
backend/pano_platform/floorplan/fusion_rooms.py
tests/test_floorplan_fusion_openings.py
tests/test_floorplan_fusion_topology.py
```

任务：开口投影WallEdge；匹配CV缺口/门弧/门扇/窗线；检查物理宽度、两侧房间、重叠；生成确认/候选/拒绝Reason Code；房间沿WallGraph闭合、正交化并检查拓扑。

DoD：未经WallEdge/Room验证的开口不会进入LayoutRevision；房间不闭合时项目为NEEDS_REVIEW。

## 3. 端到端与评测

### [进行中] T08 实现parse命令编排

依赖：T01–T07。

目标文件：

```text
backend/pano_platform/floorplan/parse.py
backend/pano_platform/facade.py
scripts/parse_one.py
tests/test_parse_pipeline.py
```

流程：输入门禁 → CV → VisionInput → Tuzi → Schema → Fusion → QA → 工件发布。

任务：接入Command Facade；阶段工件和traceId；支持 `OFF/SHADOW/ASSISTED`；Tuzi不可用转CV+NEEDS_REVIEW；支持取消、Deadline和旧Revision抑制。

DoD：单命令产出完整工件谱系；任一阶段失败都不会发布错误Revision。

### [进行中] T09 建立离线基准与Prompt回归

依赖：T08、完整真值。

目标文件：

```text
scripts/benchmark_floorplan.py
tests/fixtures/floorplan-golden/
docs/data/floorplan-v2-benchmark.json
```

任务：户型族隔离切片；分别测CV、Tuzi Draft、融合结果；输出全部P0指标、JSON有效率、延迟、成本、复核率；模型/Prompt变化跑同一冻结集。

DoD：融合留出集达到方案全部门槛，否则保持SHADOW/ASSISTED。

### [进行中] T10 完成安全、隐私和成本控制

依赖：T03、T05、T08。

任务：Secret挂载、日志脱敏、第三方处理披露、用户授权、保留删除、缓存级联、预算/日限额、SSRF/超大图防护、失败/成本告警。

DoD：安全测试通过；删除能清理所有视觉工件；超预算任务不入队。

## 4. 发布阶段

### [未开始] T11 Shadow运行

依赖：T08–T10。

- 至少30张PoC图同时跑当前结果和v2；
- VLM不影响当前Revision；
- 记录质量、成本、延迟、Schema有效率和漂移。

退出标准：Schema有效率100%，无泄密/错Revision，融合稳定优于纯CV。

### [未开始] T12 Assisted发布门

依赖：T11。

- VLM只影响候选与复核；
- 完成Tuzi不可用、模型消失、预算耗尽、删除演练；
- 留出集达到P0门槛；
- 产品、隐私、安全、成本共同评审。

通过后只能切到 `ASSISTED`；`ON`另开发布评审。

## 5. 当前阻断与明确不做

当前阻断：

- [x] Windows用户级 `TUZI_API_KEY` 已配置；部署环境仍需用Secret注入；
- [x] `/v1/models` 已确认当前Key可见 `gemini-2.5-pro/gemini-2.5-flash`（2026-08-28）；
- [x] 当前部署模型 `gemini-3.5-flash` 完成图片 + strict json_schema 探针，并用 `fp-101` 完成一次真实严格草案 Smoke；Tuzi 网络/上游仍存在间歇性 SSL/400，继续按不可用降级处理；
- [ ] 墙、房间、窗真值补足；
- [ ] 样本扩到30张PoC，发布前独立集≥200张。

## 6. 2026-08-29 实施快照

- `POST /api/v1/commands/parse` 已注册；接收 `imageBase64`、`visionMode` 和显式 `allowExternalVision`；未授权时只跑本地CV；
- Parse结果按 `projectId/taskId` 原子保存，可由 `GET /api/v1/projects/{projectId}/parse-results/{taskId}` 查询；
- Tuzi Adapter 对缺失的模型自报 `confidence` 保守记为0，坐标范围、枚举、自交、数量和额外字段仍严格拒绝；`confidence` 不参与发布门；
- Schema失败会带不含模型原文的结构化错误摘要，并进行一次带校验反馈的有限纠错；
- 付费真实Smoke改为显式 `RUN_TUZI_LIVE_TEST=1`，常规测试不再意外调用第三方；
- 当前验证：`197 passed, 2 skipped`；Viewer生产构建通过；
- 当前离线基准仍只有1个Golden：Room mIoU `0.489`、Room Count Accuracy `0`、Openings F1 `0`，因此 `releaseReady=false`；
- 桌面9图已有来源、SHA和训练/验证/测试切片，但原58门标注源文件当前不在桌面或仓库，无法诚实补齐墙/房间/窗真值。

## 7. 2026-08-29 上传到全景纵向切片

- [x] 新增 `POST /api/v1/pipeline/upload`，支持PNG/JPEG/WebP、10MB门禁、项目ID校验和显式外部视觉授权；
- [x] 将通用WallGraph、闭合房间和已确认开口转换为预览 `LayoutRevision`、`DerivedRoomSet`、`SceneGeometry`；
- [x] 从矢量墙/门窗洞和房间polylabel相机确定性生成每房间 `2048×1024` 结构ERP、1K代理和缩略图；
- [x] image-2成功后接入环绕感知 Real-ESRGAN：左右环绕填充、512分块、重叠融合，输出 `6144×3072` 及4K/2K/1K代理；工具缺失或失败时保留AI原图并记录房间级失败；
- [x] 发布动态 `PanoManifest`，Viewer支持 `?manifestUrl=/artifacts/...` 加载并拖动查看；
- [x] 新增 `/?pipeline=1` 上传页；显示每个识别阶段、质量状态、房间数和Viewer入口；
- [x] image-2默认逐个装修所有闭合房间；允许用户关闭；单房失败只回退该房间结构ERP；
- [x] 每个房间的 image-2 请求加入建筑开口白名单（door/window数量）和“禁止新增房门、墙洞、衣柜/橱柜门样板”约束，避免把自由设计当成户型识别；
- [x] 使用 `fp-101` 跑通真实纵向链：视觉草案9房/14开口，融合6个闭合房间/1个严格确认开口，生成6场景全景；结果保持 `needsReview/previewReady`；
- [ ] Tuzi image-2本轮因中转站持续SSL EOF未完成在线输出验证；结构全景链不受影响。
- [x] 修复 API 子进程调用 Real-ESRGAN 时缺少 `PYTHONPATH` 导致所有房间超分失败的问题；已在无外部环境继承的等价服务进程中验证输出四档尺寸。

## 8. 2026-08-31 WallGraph与发布门加固

- [x] 修复墙带厚度错误：旧实现返回最常见的绝对x/y坐标，现改为沿墙逐截面统计跨墙像素跨度并取众数；新增“同厚度位于不同页面坐标仍返回10px”回归测试；
- [x] 修复墙端点向右吸附最远正交轴的问题，左右两侧统一选择最近合法轴；新增多候选轴回归测试；
- [x] 收紧房间发布门：任一房间存在未吸附、面积不自洽、未闭合或比例尺待复核时，`roomsPublished=false`，仍可保留Preview但不会作为确认Revision；
- [x] Tuzi Review请求现在携带冻结的候选JSON，并拒绝候选数量变化、端点移动、夹带房间/比例尺；
- [x] 检查 `finish_reason`，`length/content_filter/safety` 响应不再把可解析的半截JSON当成有效草案；
- [x] 桌面9图重跑CV结构统计：修复墙厚/远轴吸附后实墙段11～32，旧端点假缺口被移除；
- [x] 实现同墙共线实线段之间的物理缺口模型：只保留300～3000mm缺口，生成独立Virtual WallEdge；小于物理下限的断线噪声忽略，未确认缺口默认封墙；
- [x] 开口仲裁优先匹配Virtual缺口而非重叠实墙，并使用CV完整缺口宽度，不再信任VLM粗端点宽度；
- [x] 增加房间两侧拓扑门：无邻接房间、同一房间跨墙两侧、两个室内房间之间的窗均拒绝；入口门允许一侧房间一侧室外；
- [x] `fp-101`真实ASSISTED回归：CV为17段/3缺口，VLM为9房/15开口，最终1门通过同墙、物理宽度和两侧房间拓扑；其余14个拒绝，整体仍为`needsReview`；
- [x] 窗口不再以“任意Virtual缺口”为证据：必须同时存在物理缺口、VLM `parallelLines`或CV平行细线组、合理宽度及室内/室外拓扑；
- [ ] 下一步仍需用恢复的9图门窗空间真值计算真正Precision/Recall/F1；当前不得把单图确认1门表述为门窗F1提高；
- [ ] 单Golden基准仍为Room mIoU `0.489`、Room Count Accuracy `0`、Opening F1 `0`，`releaseReady=false`。

## 9. 2026-08-31 T04/T05主链接入

- [x] Parse视觉阶段强制校验Magic Bytes、字节/像素上限并清除EXIF；本批关闭裁白边和增强，保证VLM归一化坐标仍对应原图，不伪造尚未实现的Crop Transform；
- [x] Data URI按处理后字节的真实PNG/JPEG/WebP Magic生成MIME，不再把JPEG错误标记为PNG；
- [x] `VisionJobKey`已串入主Parse，覆盖处理后图Hash、Provider/Base URL、模型、Prompt、Draft Schema和预处理策略；
- [x] 同一服务进程内缓存已验证`VlmDraft`与响应Hash，命中时不再调用Tuzi；缓存证据记录`jobKey/cacheHit/inputArtifact`；
- [ ] 当前缓存仍为单进程、最多256项、TTL 6小时；多Worker并发共享、租约、等待同一in-flight结果和重启恢复仍归T05/P0-11异步任务批次；
- [ ] 裁白边/结构增强仍未进入生产，因为必须同时持久化Crop Transform并把VLM坐标映射回原图后才能开启。

## 10. 2026-08-31 窗口证据与空间评测

- [x] 在annotation层沿物理缺口邻域检测至少两组长平行细线；覆盖率不足、只有单线或无物理缺口均不构成CV窗口证据；
- [x] 门、窗、门洞使用分类型证据门：门需`doorArc/doorLeaf`，窗需`parallelLines`，开放门洞只使用物理缺口；
- [x] `fp-101`真实回归中6个窗口提议全部因无物理缺口、无法投影或无邻接房间而拒绝，未产生窗口误发布；整体仍为`needsReview`；
- [x] Opening F1从“按类型数量凑对”改为类型+空间端点一对一匹配，支持端点反向；同类型但位置超出20px容差计为FP+FN；
- [ ] 单Golden空间Opening F1仍为0；需要完整多图真值后才能校准20px容差并按图纸分辨率/墙厚归一化。

明确不做：

- 不把第三方开源权重引入生产；
- 不解析VLM自由文本落库；
- 不把VLM自陈confidence当发布门；
- 不在浏览器调用Tuzi；
- 不自动选择模型列表中的任意替代模型；
- 不在未过留出集时宣传“自动识别率90%”。
