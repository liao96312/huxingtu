# 户型图识别 v2 详细执行 TODO

> 对应方案：[floorplan-recognition-v2.md](./floorplan-recognition-v2.md)
> 原则：一次只推进一个可验证切片；任何视觉结果都不能绕过几何与质量门。

> **2026-09-07 当前执行口径：**功能完整性和自动识别优先。39图无人干预结构链已完成；下一顺序为：30图高误差样本的房间外轮廓/虚拟墙 → 门窗剩余关联 → Tuzi在线整批恢复。程序化家具、正式云部署、T09正式F1/大样本发布门、T10安全成本、T11 Shadow和T12发布门统一后置。后文历史指标保留为证据，不代表当前优先级。

## 0. 执行顺序

```text
T00 范围与数据 → T01 Tuzi能力 → T02 契约 → T03 Adapter
→ T04 输入/Prompt → T05 证据/缓存 → T06 F1/F2 → T07 F3/F4
→ T08 编排 → T09/T10/T11/T12（后置）
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
- [x] 登记并固定官方 ResPlan `e2b78fe` 数据版本、字节数、SHA-256 与 CC BY 4.0 许可；第三方 pickle 只允许经过哈希校验和受限反序列化导入；
- [x] 将 ResPlan test split 矢量几何转换为项目 `PNG + *.truth.json`，并接入无人干预上传链计数回归；派生大文件只存 `tmp/`，不进入 Git；

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

### [后置] T09 建立离线基准与Prompt回归

依赖：T08、完整真值。

目标文件：

```text
scripts/benchmark_floorplan.py
tests/fixtures/floorplan-golden/
docs/data/floorplan-v2-benchmark.json
```

任务：户型族隔离切片；分别测CV、Tuzi Draft、融合结果；输出全部P0指标、JSON有效率、延迟、成本、复核率；模型/Prompt变化跑同一冻结集。

DoD：融合留出集达到方案全部门槛，否则保持SHADOW/ASSISTED。

### [后置] T10 完成安全、隐私和成本控制

依赖：T03、T05、T08。

任务：Secret挂载、日志脱敏、第三方处理披露、用户授权、保留删除、缓存级联、预算/日限额、SSRF/超大图防护、失败/成本告警。

DoD：安全测试通过；删除能清理所有视觉工件；超预算任务不入队。

## 4. 发布阶段

### [后置] T11 Shadow运行

依赖：T08–T10。

- 至少30张PoC图同时跑当前结果和v2；
- VLM不影响当前Revision；
- 记录质量、成本、延迟、Schema有效率和漂移。

退出标准：Schema有效率100%，无泄密/错Revision，融合稳定优于纯CV。

### [后置] T12 Assisted发布门

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
- [x] 公共结构真值补足：ResPlan 提供墙、房间、门、窗和固定 test split；已接入本地合成栅格回归；
- [后置/可选] 真实中文户型图域的墙、房间、窗真值不是当前实施前置；ResPlan 真值已足够支撑通用结构回归，桌面图真值只用于后续域内准确率核验；
- [x] 无人干预链路样本已扩到30张ResPlan，加桌面9图共39/39生成Viewer；发布前独立集≥200张继续归后置发布门。

## 6. 2026-08-29 实施快照

- `POST /api/v1/commands/parse` 已注册；接收 `imageBase64`、`visionMode` 和显式 `allowExternalVision`；未授权时只跑本地CV；
- Parse结果按 `projectId/taskId` 原子保存，可由 `GET /api/v1/projects/{projectId}/parse-results/{taskId}` 查询；
- Tuzi Adapter 对缺失的模型自报 `confidence` 保守记为0，坐标范围、枚举、自交、数量和额外字段仍严格拒绝；`confidence` 不参与发布门；
- Schema失败会带不含模型原文的结构化错误摘要，并进行一次带校验反馈的有限纠错；
- 付费真实Smoke改为显式 `RUN_TUZI_LIVE_TEST=1`，常规测试不再意外调用第三方；
- 当前验证：`205 passed, 2 skipped`；Viewer生产构建通过；
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

- [x] Parse视觉阶段强制校验Magic Bytes、字节/像素上限并清除EXIF；已启用裁白边并记录`cropOffset/processedWidth/processedHeight`，VLM草案回映到原图坐标；结构增强仍关闭；
- [x] Data URI按处理后字节的真实PNG/JPEG/WebP Magic生成MIME，不再把JPEG错误标记为PNG；
- [x] `VisionJobKey`已串入主Parse，覆盖处理后图Hash、Provider/Base URL、模型、Prompt、Draft Schema和预处理策略；
- [x] 同一服务进程内缓存已验证`VlmDraft`与响应Hash，命中时不再调用Tuzi；缓存证据记录`jobKey/cacheHit/inputArtifact`；
- [ ] 当前缓存仍为单进程、最多256项、TTL 6小时；多Worker并发共享、租约、等待同一in-flight结果和重启恢复仍归T05/P0-11异步任务批次；
- [x] 裁白边安全档已进入主Parse；`fp-101`真实Tuzi Smoke通过，裁切输入和原图坐标映射均有单测；
- [ ] 结构增强仍未开启，需独立证明细线召回提升且不引入伪结构；

## 12. 2026-08-31 CV-only自动预览兜底

- [x] `run_cv_structure`持久化WallGraph、墙宽、比例尺和原图尺寸证据；
- [x] Tuzi不可用或显式OFF时，自动用`polygonize_rooms`闭合CV房间，生成Preview Layout/Rooms/SceneGeometry和结构ERP，不再因视觉服务波动直接无结果；
- [x] CV-only房间统一标记`CV_ONLY_ROOM`和`needsReview`，不进入确认Revision；
- [ ] CV-only只提供结构兜底，不代表房间类型、门窗和比例尺正确；仍需多图真值和T11 Shadow对照。

## 13. 2026-08-31 房间坐标吸附加固

- [x] WallGraph记录实测`wall_width_px`，F1吸附优先使用`1.5×wall_width_px`，不再用墙轴间距推导容差；
- [x] F4房间正交化只在墙宽容差内吸附，远离墙轴的VLM顶点保持原坐标，避免跳到错误平行墙；
- [x] 修正CV房间轮廓`x→vertical axis / y→horizontal axis`坐标交叉错误；不等距轴回归已覆盖；
- [ ] 多图真值仍未恢复，房间mIoU、房间数准确率和邻接F1尚未重新计算，不能据此宣称识别率提升。
- [x] 对墙带包围框中缺失的外轮廓边补充Boundary WallEdge，保证CV洪水填充能形成房间；只补外框，不参与同墙门窗缺口候选；
- [x] 9图CV-only房间数进一步变为`2/1/3/2/2/2/3/0/6`，`fp-102`由0个恢复为1个；
- [x] 外轮廓策略改为检查边界轴覆盖长度，不把“中段有轴”误当整条外墙；`fp-108`恢复1个CV闭合房间；

## 14. 2026-08-31 CV房间闭合微缝兜底

- [x] 在`polygonize_rooms`栅格洪水填充前增加按实测墙宽计算的小形态闭合，只修扫描/形态学产生的角点微缝；
- [x] 9图CV-only房间数从`1/0/3/2/1/1/2/0/1`变为`2/0/3/2/1/1/2/0/5`，`fp-101`由1个房间恢复为2个；
- [ ] `fp-108`虽恢复1个CV闭合房间，但复杂凹形房间仍需真值验证，不能把包围框闭合当作完整户型拓扑；
- [ ] 当前改善仅是结构兜底，不代表房间类型或房间mIoU提升，仍需真值集验证。

## 15. 2026-09-01 外轮廓覆盖策略

- [x] 外框补边现在要求已有边覆盖包围框长度至少85%，仅存在中段轴时仍补整条Boundary WallEdge；
- [x] `fp-102`和`fp-108` CV-only均能形成至少1个房间，避免因外框漏检导致整图无结果；
- [ ] Boundary WallEdge对复杂凹形户型只提供保守外壳，房间分割、房间类型和门窗拓扑仍需VLM/真值共同验证。

## 16. 2026-09-01 房间重叠拓扑门

- [x] F4闭合后计算房间交叠面积；同类型且较小房间85%以上被覆盖时自动抑制重复项；
- [x] 不同类型房间或非重复房间交叠达到35%时，相关房间全部转`ROOM_OVERLAP_CONFLICT/needsReview`，不得进入3D；
- [x] Parse工件新增房间级诊断，记录闭合/复核状态及Reason Codes；
- [x] `fp-101`真实回归遇到Tuzi `VISION_SCHEMA_INVALID`时严格拒绝草案并走CV-only兜底，未放宽Schema；
- [ ] 阈值仍需冻结真值集校准，尤其是客餐厅开放空间与阳台/厨房嵌套边界。

## 17. 2026-09-01 房间文字类型纠错

- [x] 复用Tuzi读取的房间文字，建立`卧室/主卧/厨房/卫生间/阳台/书房/客厅/餐厅/客餐厅`到冻结枚举的确定性映射；
- [x] 图纸文字与模型枚举冲突时以明确文字纠正类型，并记录`ROOM_LABEL_TYPE_CORRECTED`；
- [x] 无文字或文字不在白名单时保留模型枚举，不按面积/位置猜房间类型；
- [ ] 当前环境无本地中文OCR；Tuzi不可用时CV-only仍只能输出`other`，后续若引入OCR必须独立评测中文标签准确率。

## 11. 2026-08-31 上传任务异步化PoC

- [x] 上传接口改为立即返回`202 + taskId + pollUrl`，后台线程执行识别、融合、逐房AI和超分；
- [x] 任务状态统一返回`PROCESSING/DONE/NEEDS_REVIEW/FAILED`、`progress`、结果和错误摘要；前端自动轮询，提交请求不再承载整条长链；
- [x] 线程级失败隔离：单房AI/超分失败写入结果清单，其余房间继续完成；
- [ ] 当前任务表仅为单进程内存，重启会丢任务查询；未接PostgreSQL事实源、Redis队列、租约、Heartbeat、取消和断点恢复，不能作为生产异步实现；
- [ ] 仍需限制并发项目/房间数、预留成本和总Deadline，避免全房默认AI在单机线程池中无界排队。

## 10. 2026-08-31 窗口证据与空间评测

- [x] 在annotation层沿物理缺口邻域检测至少两组长平行细线；覆盖率不足、只有单线或无物理缺口均不构成CV窗口证据；
- [x] 门、窗、门洞使用分类型证据门：门需`doorArc/doorLeaf`，窗需`parallelLines`，开放门洞只使用物理缺口；
- [x] `fp-101`真实回归中6个窗口提议全部因无物理缺口、无法投影或无邻接房间而拒绝，未产生窗口误发布；整体仍为`needsReview`；
- [x] Opening F1从“按类型数量凑对”改为类型+空间端点一对一匹配，支持端点反向；同类型但位置超出20px容差计为FP+FN；
- [ ] 单Golden空间Opening F1仍为0；需要完整多图真值后才能校准20px容差并按图纸分辨率/墙厚归一化。

## 18. 2026-09-01 AI全景结构一致性门

- [x] image-2输出在进入本地超分和PanoManifest前，与确定性结构ERP执行低频环绕边缘代理校验；检查参考召回、候选精度、边缘密度漂移和长直线密度漂移；
- [x] 空白、不可读或明显偏离结构的AI结果自动回退该房间结构全景，不阻断其他房间；
- [x] 每个成功解码的AI房间写入`ai-structure-qa.json`，明确`semanticIoU/semanticF1=null`，不把代理指标冒充语义评测；
- [x] 通过的AI结果才进入Real-ESRGAN；QA拒绝不会继续放大疑似错误结构；
- [ ] 该代理门不能识别家具线条与真实门扇的语义差异；仍需带门窗真值的语义分割/空间F1留出集才能冻结生产阈值。

## 19. 2026-09-01 无人工干预连续交付准则

- [x] 将“识图→建模→全房间生图→超分→可拖动全景”定义为正常图片的自动连续链路，人工标注/复核不再作为出图前置条件；
- [x] 视觉不可用时自动使用CV结构或外壳预览，房间级AI失败自动隔离并回退，不阻断其他房间；
- [x] 超分失败保留AI原图，AI结构QA失败回退结构ERP；所有降级都仍生成可浏览PanoManifest；
- [x] 只有输入门禁失败（空文件、格式/魔数非法、超限）才允许任务失败；有效图片即使识别质量不足也继续生成低置信度预览；
- [x] 完成30张ResPlan＋桌面9图连续链路统计：39/39无需人工放行生成Viewer；本轮使用结构全景，不包含付费image-2成功率。

实现说明：当CV墙图为空、闭合房间为空或比例尺不可用时，主链自动生成带`AUTO_ENVELOPE_FALLBACK`/`AUTO_SCALE_ESTIMATE`证据的低置信度外壳预览，并继续建模、出ERP和发布Viewer；输入门禁失败仍按无效输入结束。

- [x] 房间级结构ERP渲染异常自动隔离，使用中性2:1保底ERP并继续生成其余房间；异常房间写入`structural-fallback-rooms.json`，不把单房异常升级为项目失败；

## 21. 2026-09-01 门弧候选去除指南针误检

- [x] 复用现有Hough门弧检测器增加闭合圆形图例过滤：检测小半径、全圆暗边缘覆盖的指南针/方向箭头，不再作为门弧候选；
- [x] `fp-109`回归验证左下指南针候选被移除；9图评价页左下区域候选数均为0；
- [x] 新增回归测试，确保已知真实门弧仍保留；
- [ ] 其余门弧候选仍需结合用户逐项评价和完整门窗真值，不能仅凭候选数量宣称F1提升。

## 22. 2026-09-01 用户门窗评价回灌

- [x] 接收用户导出的`door-window-feedback.json`，校验其中`reportHash`对应当时评价页版本；
- [x] 明确记录漏检重点：`fp-103`阳台/厨房门、`fp-105`三个卧室门、`fp-106`入户花园门；
- [x] 明确记录`fp-101`存在重复候选和位置偏差，不能按圆圈数量计门；
- [x] Hough门弧候选阈值从20放宽到18，固定OpenCV随机种子、稳定排序并收紧相邻重复合并；评价页统一使用直接灰度解码；
- [ ] 当前反馈仍是部分逐项评价和整图备注，不足以构成9图完整真值；重跑后的候选编号变化后，旧评价不自动迁移，避免错配；
- [ ] 继续用本页确认漏检/误检后，生成正式门窗真值并计算空间端点Precision/Recall/F1。

## 23. 2026-09-01 CV门弧进入自动建模链

- [x] `run_cv_structure`持久化现有Hough门弧提议，使用直接灰度解码和固定RNG；
- [x] 新增CV门弧仲裁：最近WallEdge投影、门宽范围、门弧分数和房间两侧采样全部通过后，自动加入Preview Layout的门洞；
- [x] 门扇覆盖墙中心线导致无物理缺口时，允许高分门弧在实体WallEdge上估计门宽；
- [x] CV自动门证据写入`automatic-cv-openings.json`，确认和拒绝均留痕；没有拓扑证据的候选继续拒绝，不强行开洞；
- [x] 对虚拟物理缺口上的高分门弧增加自动继续路径，即使CV外框暂时把两侧采成同一房间也可开洞，并记录`CV_ARC_SHARED_ROOM_PHYSICAL_GAP`；
- [ ] 实体WallEdge上的同房两侧仍拒绝；CV房间面过粗导致的其他真实内门仍需更细房间分割或Tuzi房间草案。

## 24. 2026-09-01 CV房间墙体Mask补强

- [x] `run_cv_structure`保存原始`wallMask`，`polygonize_rooms`在绘制规范化WallGraph前合并该Mask，避免门扇/扫描断线让内部隔墙消失；
- [x] 房间轮廓只在`1.5×wall_width_px`范围内吸附到轴线，远离轴线的Mask轮廓保留自身坐标，避免所有房间被吸成同一外框；
- [x] 9图CV-only仍全部生成Viewer，房间面数量提升到`4～10`，并改善CV门弧两侧房间采样；
- [ ] 外框和复杂凹形户型仍需逐图检查，房间数量增加不等于房间类型或面积准确率达标。

## 25. 2026-09-01 超宽缺口多门自动拆分

- [x] 物理缺口超过单门上限时，不再把整段当作一扇超宽门；按各门弧中心和半径生成独立子区间；
- [x] 同一WallEdge上的相邻门区间自动裁边为不重叠，进入`SceneGeometry`前即满足开口互斥约束；
- [x] `fp-107` CV-only自动门从0恢复为3，9图继续全部生成Viewer；
- [x] 任一拆分后门宽低于物理下限仍自动拒绝，不通过几何层异常阻断项目。

## 26. 2026-09-01 CV-only外窗自动发布

- [x] 复用现有窗口平行线检测，CV-only无需VLM候选即可检查物理缺口邻域的两组长平行细线；
- [x] 只有物理宽度、平行线和室内/室外拓扑同时通过的候选进入Preview Layout；内窗、同房两侧和无邻接房间自动拒绝；
- [x] 自动窗与VLM开口、CV自动门统一做WallEdge区间去重，窗口证据优先于同位置门弧；
- [x] 9图全部继续生成Viewer，当前保守确认`fp-104`的1个外窗，其余缺口不批量发布为窗。

## 27. 2026-09-02 风格选择进入自动生图链

- [x] 将既有8种`StyleCatalog`提升为通用`datasets/style-catalog.v1.json`，不再绑定`fp-001`；
- [x] 新增`GET /api/v1/pipeline/styles`，上传接口接收并校验`styleId`；
- [x] 同一项目的全部房间共享同一个`StyleContext`，风格、色板、材质和Prompt后缀进入image-2请求及幂等键；
- [x] 上传页提供现代、中式、北欧、奶油风、法式、混搭、轻奢、侘寂选择，默认现代；
- [ ] 当前风格只约束AI精渲，程序化家具布局/资产套系仍未接入。

## 29. 2026-09-02 房间热点导航

- [x] 复用 `LayoutRevision.openings`、墙段和 `DerivedRoomSet` 房间多边形，在确认门洞中点同时接近两个房间边界时生成双向房间热点；
- [x] 热点写入 `PanoManifest.scenes[].hotspots`，包含目标场景、中文标签、朝向 yaw/pitch；目标不存在或自指时由 manifest 契约拒绝；
- [x] Viewer 增加“门洞导航”按钮，点击直接切换目标房间；场景缩略图导航继续保留，热点生成失败不会阻断全景发布；
- [x] ResPlan 回归验证生成的 Viewer 中已出现每场景0～2个房间跳转；单侧入口、窗户、无拓扑证据的开口不生成热点；
- [ ] 当前热点是场景级导航按钮，不是投影到球面的三维图标；需要真实用户评审后再决定是否增加随 yaw/pitch 移动的空间标记。

## 30. 2026-09-02 Tuzi image-2稳定性恢复

- [x] image-2会话支持 `TUZI_HTTP_PROXY`，其次沿用标准 `HTTPS_PROXY/HTTP_PROXY`，代理只注入该会话，不修改系统/Git全局配置；
- [x] 连接/读超时、408/425/429/500/502/503/504使用有上限指数退避并尊重`Retry-After`；400、401/403、内容拦截和输入错误不重试；
- [x] 保留请求级`Idempotency-Key`，POST重试复用同一键；单房请求最终失败仍写入失败列表并保留结构ERP，其他房间继续；
- [ ] 当前未做在线 image-2 付费回归；等待中转站稳定或用户显式开启 live smoke 后，再验证真实返回尺寸、请求ID、失败码和整批房间成功率。

## 31. 2026-09-02 任务状态持久化与重启恢复

- [x] 上传任务在提交线程池前将状态和原图写入 `PANO_DATA_ROOT/_pipeline_tasks/<taskId>/`，状态文件采用临时文件原子替换；
- [x] 轮询接口在内存缓存缺失时从磁盘恢复任务；服务启动扫描 `PROCESSING` 任务并自动重新提交，继续使用原项目、风格和外部视觉开关；
- [x] 重启恢复回归验证：清空内存后可读回进度，并重新提交持久化输入；缺失输入会变成明确 `FAILED`，不会静默卡死；
- [ ] 当前仍是单机持久化，未接 PostgreSQL 任务事实源、跨实例租约/Heartbeat、取消和分布式队列；云多实例发布前必须升级该层。

## 32. 2026-09-02 24小时临时分享（单实例PoC）

- [x] 新增 `POST /api/v1/projects/{projectId}/shares`：默认 TTL 86400 秒，生成 256-bit 随机 Token，只持久化 Token SHA-256、项目和 manifest revision；
- [x] 新增 `GET /api/v1/shares/{token}`：Viewer 通过 `?shareToken=` 读取只读 PanoManifest；过期、撤销或 manifest 不存在统一返回 410；
- [x] 新增 `DELETE /api/v1/projects/{projectId}/shares/{shareId}`：撤销立即生效且重复撤销幂等；原始 Token 不写入状态文件；
- [x] `shareOnly/ephemeralProject` 字段已进入分享记录，默认 `shareOnly`；短期测试可直接使用相对分享 URL；
- [ ] 当前仍为单实例本地分享：资源 URL 仍走宿主静态 artifacts，尚未接私有 Bucket/短时签名 URL、CDN Purge、跨租户鉴权和物理删除 SLO；云多实例发布前升级到方案中的 ShareGrant 数据库实现。

## 33. 2026-09-03 项目文件夹临时分享隧道

- [x] 复用项目内 `tools/cloudflared.exe` 和 Vite 的 `.trycloudflare.com` Host 白名单，不要求域名、Cloudflare账号或云服务器；
- [x] 新增 Windows 主入口 `scripts/local_share.ps1`：自动检查/启动本地 API 和 Viewer，探测 `127.0.0.1:7897`，代理失败时有限重试并最终尝试直连；
- [x] 支持 `start/url/stop`，只停止 PID 文件对应且路径匹配项目 cloudflared 的进程；Git Bash 脚本收敛为 PowerShell 包装层；
- [x] 传入 `ProjectId` 时自动调用24小时 Share API并输出客户 Viewer URL；Viewer 分享按钮也会自动创建24小时 Token；
- [x] 公网实测：Viewer、只读 share manifest、全景图片均返回 HTTP 200，项目包含5个场景；
- [ ] Quick Tunnel 不保证固定地址和持续在线；Token 24小时后失效，但隧道进程需手动 `stop` 或随电脑/进程退出销毁。这是明确的短期测试方案，不升级为云部署承诺。

当前后置：

- [ ] 程序化家具布局和 GLB 资产包暂不做；
- [ ] 正式云部署、私有对象存储、CDN和多实例任务系统暂不做。

## 34. 2026-09-03 门弧召回实验与拒绝证据

- [x] 用 ResPlan 76个真值门和桌面9图对照 Hough 最大半径50/60/70/80：60档原始命中由64增至72，但桌面候选由54增至69；
- [x] 放大半径实验在 `fp-107` 把卫生间门弧错误吸到左外墙窗段，未进入生产代码；
- [x] “距离优先再选虚拟缺口”实验虽找回 `fp-106` 一门，但丢失 `fp-104`、`fp-107` 两个真实门，未进入生产代码；
- [x] “半径等于门宽”的单向开洞实验使 ResPlan 严格确认开口从24降至12，未进入生产代码；
- [x] `automatic-cv-openings.json` 的拒绝项补齐 `type/startPx/endPx/widthMm/sideRoomIds/reasonCodes`，后续不再靠候选数量猜根因；
- [ ] 下一步把 Hough 半径截断、门弧象限、门扇端点和 WallEdge 关联作为一个联合问题解决；任何改动必须同时保持桌面真实门和 ResPlan 链路，不再单独放宽半径/距离阈值。

## 28. 2026-09-02 ResPlan公共真值接入

- [x] 通过本机 `127.0.0.1:7897` 代理从官方 GitHub 固定提交下载 `ResPlan.zip`；校验大小 `100106537` 字节、SHA-256 `f718de8865e51bbe93b49b584798e3c536ed6b4b8a5d32f01b56812f389aeb46`；
- [x] 新增 `datasets/external/registry.json`，登记来源、版本、许可证和生产使用边界；CubiCasa5K 因 CC BY-NC 4.0 只登记为研究对照，不进入商业训练/运行路径；
- [x] 新增 `scripts/prepare_resplan_truth.py`：受限 Unpickler 只开放 Shapely WKB 与 NumPy scalar/dtype，拒绝未知全局对象；从官方矢量门几何绘制标准门扇/门弧并生成冻结标注格式；
- [x] 从官方 test split 生成首批10图：83个房间、162个门窗开口；新增 `scripts/run_resplan_regression.py`，实际跑“上传→识别→建模→全房间结构全景→Viewer”；
- [x] 10/10 样本无人干预生成 Viewer；修正转换margin后当前CV预测89个房间、19个严格确认门窗开口。这里只保留计数差与链路成功率，不计算/宣传正式F1；
- [ ] 将 ResPlan 门弧半径、墙厚和房间复杂度分桶加入门弧/房间回归，优先解决开口漏检与复杂户型房间过分割；不得通过取消WallEdge、物理宽度或房间拓扑门来刷召回；
- [x] ResPlan 无原始房产户型图但有完整矢量真值；已用于通用墙/房间/门窗结构回归；
- [后置/可选] 真实中文图域的桌面9图/用户样本只做域内防退化检查，不是继续主链或获得准确率声明的前置条件。

## 35. 2026-09-03 房间微碎片过滤

- [x] 修复 polygonize_rooms 默认最小面积单位错误：原值0.5按实现实际是0.5平方毫米，等同未过滤；
- [x] 默认改为50,000平方毫米（0.05平方米），只排除不可能成为房间的墙缝微碎片；未采用0.5平方米激进阈值，避免比例尺自举误差伤到有效小空间；
- [x] ResPlan 首批10图预测房间由112降至108，真值为83；桌面9图由57降至55；
- [x] ResPlan 10图和桌面9图共19/19仍无人干预生成Viewer；
- [x] 尝试按门弧圆盘清除wallMask后，ResPlan房间由108降至76并合并真实房间，已撤销；
- [ ] 剩余大块额外空间不是微碎片，主要是外轮廓外的封闭空白区；下一步从建筑主外轮廓/房间连通拓扑识别，不再继续抬高面积阈值。

## 36. 2026-09-04 ResPlan画布margin修复

- [x] 修复转换器只缩放到预留尺寸、却没有把几何平移到48px margin的问题；旧生成图贴住上/左边界，污染输入边界与外框回归；
- [x] 转换Manifest新增 converterVersion=v2-margin48，避免旧贴边派生数据与新基线混用；
- [x] 重新生成官方test首批10图并跑完整链：预测房间由108降至89，真值83；逐图房间计数绝对误差由27降至22；
- [x] 原始Hough在76个真值门铰附近的覆盖由64变为65，没有因margin修复降低；严格确认开口当前为19，后续继续由WallEdge/拓扑层处理；
- [x] 10/10 ResPlan和9/9桌面图继续无人干预生成Viewer；
- [ ] 过分割当前集中在 resplan-00071 与 resplan-00080，各多6个空间；下一步按这两个样本追踪凹形外轮廓，不对全部图统一删整边。

## 37. 2026-09-04 Tuzi结构化响应兼容与实探

- [x] 复查Tuzi官方文档：Chat接口用于图片理解，但页面未声明Chat的response_format/json_schema；Responses接口声明图片与结构化JSON，JsonSchemaFormat采用type/name/schema/strict直接结构；
- [x] 实测当前gemini-3.5-flash：Responses纯文本返回200和request ID；Responses内嵌data URI图片在代理、直连下均于响应头前断开，故不切主路径；
- [x] Chat图片仍可到达模型，但本次严格Schema连续3次返回非JSON，归一为VISION_SCHEMA_INVALID，不允许自由文本猜JSON；
- [x] Adapter兼容官方code/message/request_id/data网关包装，能从data内提取choices、usage、actualModel，并优先记录顶层request_id；
- [x] HTTP错误证据新增安全状态码摘要，如HTTP 403；不记录Key、Base64或模型自由文本；
- [ ] 继续使用Chat图片+本地严格校验和CV回退；只有Responses图片data URI或私有文件输入完成真实VlmDraft探针后，才允许切换endpoint。

## 38. 2026-09-04 包围壳房间的非破坏性处置

- [x] 在桌面9图中定位到 `fp-105/fp-106/fp-107` 各有一个包含至少两个内部房间的疑似包围壳；ResPlan首批10图没有同类候选；
- [x] 拒绝直接删除包围壳：实验虽把桌面房间数从55降到52，却使严格确认门从15降到12，破坏后续门拓扑和房间跳转；
- [x] 增加 `automatic-room-evidence.json`，记录疑似包围壳及其包含的房间，不依赖人工标注；
- [x] 保留全部房间参与几何、开口和回退，只把疑似包围壳排到全景列表末位；`fp-105/fp-106/fp-107` 默认场景均从 `room_001-main` 改为首个内部房间；
- [x] 回归保持：ResPlan 83个真值房间对应89个预测房间、10/10 Viewer；桌面9图55个房间、15个门、9/9 Viewer；
- [x] 已继续处理 `resplan-00071/resplan-00080` 的超长虚拟墙过分割，结果见第39节；仍只允许加入同时改善公共真值且不降低桌面门拓扑的几何证据。

## 39. 2026-09-04 超长虚拟墙上限回归

- [x] 根因定位：`resplan-00071` 存在2493mm和2955mm的共线空白，旧3000mm上限会把开放区域补成虚拟墙并切出额外房间；
- [x] 复用现有 `max_opening_width_mm` 做1800/2200/2500/3000四档联合消融，没有新增识别算法；
- [x] 1800mm档使桌面严格门从15降到13，拒绝；2200mm档使ResPlan房间总数降到75，明显欠分割，拒绝；
- [x] 采用2500mm：ResPlan预测房间由89降到83，严格开口保持19，10/10 Viewer；桌面9图保持55房间、15门、9/9 Viewer；
- [x] `resplan-00071` 从15降到13（真值9），`resplan-00080` 从13降到10（真值7）；总数等于83不代表逐图准确，也不替代正式IoU/F1；
- [ ] 下一步优先提升门窗严格确认召回；房间残余误差必须按逐图匹配/拓扑证据处理，不再根据总房间数继续调单一阈值。

## 40. 2026-09-04 门宽自举比例尺容差

- [x] 对76个ResPlan真值门追踪“Hough候选→WallEdge→宽度→拓扑→确认”全链：66个已有门弧候选，旧基线仅19个确认；主要拒绝原因是110mm墙厚自举把真实门估成约350～490mm，低于500mm物理下限；
- [x] 复用现有门弧、WallEdge和两侧房间门禁，只对强门弧的自举比例尺允许 `0.7 × 500mm - 1像素` 容差；500～1500mm物理门宽定义本身不变；
- [x] 容差确认写入 `CV_ARC_BOOTSTRAP_SCALE_TOLERANCE`，可与正常物理宽度确认分开审计；不放宽弧分数、墙距离、重复开口或房间拓扑门；
- [x] ResPlan严格确认门由19增至41，新增22个均落在公共真值门铰附近；房间保持83，10/10 Viewer；
- [x] 桌面9图严格确认门由15增至23，新增8个位置逐图核对均为可见门弧；房间保持55，9/9 Viewer；
- [x] 分析5个 `CV_ARC_NO_WALL_EDGE`：门弧中心离最近墙端44～64px，约等于门弧半径；直接放宽投影会把门洞向现有墙内错移一个门宽，未采用；
- [x] 尝试按“缺口长度/门弧半径”触发已有多门拆分：ResPlan确认仅增2，桌面 `fp-109` 丢失1个已确认真实门，已撤销；
- [ ] 剩余35个ResPlan真值门中，10个没有Hough候选，其余主要被无WallEdge、同房两侧、重复关联拒绝；下一步按拒绝原因分别处理，不再继续降低门宽容差。

## 41. 2026-09-07 ResPlan窗符号回归修复

- [x] 定位公共回归零窗确认根因：真值声明 `parallelLines`，旧转换器却只画一条窗中心线；生产窗算法要求至少两条平行线，故所有虚拟缺口都没有窗证据；
- [x] 转换器升级为 `v3-window-parallel-lines`，从同一真值长轴绘制两条相距6px的窗扇线；真值坐标、官方split和许可均不变；
- [x] 重新生成首批10图后，现有生产算法确认9个窗，9/9距对应真值窗线0.6～1.3px；另2个落在两个内部房间之间，继续由拓扑门拒绝；
- [x] 完整链保持10/10 Viewer；当前ResPlan基线为84个预测房间、40个门、9个窗，桌面9图仍为55个房间、23个门、9/9 Viewer；
- [x] 生产一致的Hough消融确认：直接扩大最大半径到55会在桌面 `fp-101` 墙角和 `fp-104` 顶部外边界新增假门，未采用；降低Hough阈值同样产生非真值确认，未采用；
- [ ] 下一步提升窗缺口覆盖：当前只有部分真值窗形成WallGraph虚拟缺口；必须先改善墙段/外轮廓缺口几何，不放宽“平行线+单侧房间”窗门禁。

## 42. 2026-09-07 连墙窗线证据恢复

- [x] 定位 `layers.annotation` 丢窗原因：形态学墙体重建会沿连通墨迹吸收接在墙端的细窗扇线，导致已存在的虚拟缺口看不到平行线；
- [x] 复用既有虚拟缺口裁剪、双平行覆盖和单侧房间拓扑，只把小范围证据源切换为原始二值墨迹；不在整图上直接把任意双线发布为窗；
- [x] ResPlan保持84房间、40门、9窗、10/10 Viewer，没有新增非真值窗；
- [x] 桌面9图保持55房间、23门、9/9 Viewer，严格窗由1增至3；新增 `fp-104`、`fp-109` 两段均为图上可见外墙窗；
- [x] 新增 `fp-109` 整链回归，锁定连墙窗线必须进入自动结果；
- [ ] 剩余窗召回瓶颈仍是WallGraph未生成对应虚拟缺口；下一步处理外边界上的局部缺口表达，不取消单侧房间拓扑门。

## 43. 2026-09-07 实墙局部窗截面识别

- [x] 拒绝全局截断墙体形态学重建：ResPlan房间84→77、桌面房间55→33，导致真实空间大面积合并，实验未进入生产代码；
- [x] 在已有实墙/边界WallEdge的局部带状区域扫描连续双线截面，只接受300～3000mm区间和恰好单侧房间；不修改WallGraph，不影响房间闭合；
- [x] 与已有门弧圆相交的区间不允许成为窗，挡住桌面 `fp-101`、`fp-108` 的门弧误报；
- [x] 同方向、墙厚范围内且沿轴重叠超过一半的区间跨WallEdge去重，避免虚拟缺口路径和实墙路径重复开窗；
- [x] ResPlan严格窗由9增至37，覆盖37个不同真值窗、零重复和零非真值；门40、房间84、10/10 Viewer保持；
- [x] 桌面9图保持55房间、23门、3窗、9/9 Viewer，没有新增门弧/墙角假窗；
- [ ] 下一步处理仍无局部双线区间的窗，优先查短窗线、外墙端点和比例尺下的最小连续长度；不得取消门弧互斥或单侧房间门。

## 44. 2026-09-07 窗拓扑采样距离校准

- [x] 将剩余49个ResPlan真值窗分桶：29个局部区间被短墙段截断到300mm以下，13个卡房间拓扑，6个无双线区间，1个被去重；
- [x] 拒绝降低300mm窗宽下限：270mm档会把桌面尺寸标注线新增为5个外窗，210/240mm误报更多；
- [x] 对窗专用的墙侧采样距离做1.5/2/2.5/3倍墙厚消融；3倍额外恢复2个不同真值窗，桌面结果不变；
- [x] 门侧采样继续保持1.5倍墙厚；窗侧单独使用3倍，避免门拓扑基线受影响；
- [x] 当前ResPlan为84房间、40门、39个不同窗、10/10 Viewer；桌面9图保持55房间、23门、3窗、9/9 Viewer；
- [ ] 下一步处理被短WallEdge截断的窗区间；需要先解决开口承载边，不得把不足300mm的碎片直接作为窗发布。

## 45. 2026-09-07 门窗分项计数回归

- [x] `run_resplan_regression.py` 在保留原门窗总数字段的同时，新增逐图 `truth/predicted Doors`、`truth/predicted Windows` 和各自计数差；
- [x] 报告新增 `totals` 汇总，当前实际结果为83/84房间、76/40门、86/39窗，10/10自动链成功；不再允许门增窗减或重复开口被总数掩盖；
- [x] 拒绝把窗宽下限从300mm降到270/240/210mm：会将桌面图外部尺寸线识别为窗；
- [x] 拒绝为单一真值收益增加“短片段合并+虚拟承载边”：仅新增1窗，却需要改动WallGraph和3D边契约；
- [x] 拒绝窗区间3点/5点沿线采样：相较中点采样无任何新增真值窗；
- [ ] 后续每个门窗改动都必须同时报告分项计数、不同真值覆盖数、桌面门窗数和Viewer成功数。

## 46. 2026-09-07 三十图无人干预链路回归

- [x] 按官方固定test split把ResPlan派生回归从10图扩到30图；继续使用固定提交、受限反序列化和 `v3-window-parallel-lines`，派生文件仅存 `tmp/`；
- [x] 30/30 ResPlan从上传、CV识别、建模、全房间结构全景到Viewer自动完成；叠加桌面9图后为39/39，无人工放行；
- [x] 30图计数基线：真值/预测房间259/289，门235/115，窗266/95；该结果只证明链路连续性和提供防退化基线，不代表正式准确率；
- [x] 墙端门洞承载边实验因候选全部为同房两侧而拒绝，未把几何对齐但拓扑不成立的门强行发布；
- [x] 已按 `resplan-00124/00145/00249/00263` 追踪矩形补边造成的小型外部空间并完成第47节过滤；剩余误差继续逐图处理，不根据总量统一删房间。

## 47. 2026-09-07 小型无墙外部空间过滤

- [x] 追踪30图房间与 `wall_boundary_*`、原始墙Mask的重合：矩形补边会在非矩形户型外部封出空白小房间；
- [x] 冻结保守证据门：房间面积不超过5㎡，且至少48%周长依赖没有真实墙墨迹的矩形补边；30图命中17个，17个与全部真值房间最佳IoU均低于0.1；
- [x] 过滤在门窗拓扑仲裁完成后执行，因此保留这些区域对开口关联的中间证据；过滤结果写入 `automatic-room-evidence.json.unsupportedBoundaryRoomIds`；
- [x] 大于5㎡的大型兜底外壳不删除，继续承担复杂户型缺失房间的保底展示；
- [x] ResPlan 30图预测房间由289降至272，真值259；门115、窗95和30/30 Viewer保持；
- [x] 桌面9图保持55房间、23门、3窗和9/9 Viewer，没有触发小型外部空间过滤；
- [x] 已继续处理0.05～0.1㎡的墙缝碎片，结果见第48节；逐图仍同时有过分割和欠分割，不继续放宽48%或5㎡门槛。

## 48. 2026-09-07 拓扑后极小房间过滤

- [x] 30图真值面积复核：259个真值房间中没有小于0.2㎡的房间；当前0.05～0.1㎡预测区域共17个，均只是墙缝或真值边角碎片；
- [x] 拒绝直接提高 `polygonize_rooms` 下限：即使0.1㎡档也使ResPlan门115降至114，说明这些碎片仍参与开口拓扑；
- [x] 保持房间多边形化下限0.05㎡，在门窗仲裁完成后才移除小于0.1㎡的交付房间；证据写入 `automatic-room-evidence.json.implausiblySmallRoomIds`；
- [x] ResPlan预测房间由272降至255，门115、窗95、30/30 Viewer保持；真值房间为259，但总数接近不代表逐图正确；
- [x] 桌面9图房间由55降至50，移除5个0.067～0.098㎡碎片；门23、窗3、9/9 Viewer保持；
- [ ] 下一步停止按面积继续过滤，转向逐图房间合并/缺失：当前ResPlan总数净少4，但高误差样本仍同时存在多房和少房。

## 49. 2026-09-08 可复现运行环境与虚拟缺口消融

- [x] 从干净`.venv`复现出依赖缺口：FastAPI上传接口缺 `python-multipart`、Tuzi图片链缺 `requests`；此前被系统Python预装包掩盖；
- [x] `pyproject.toml`固定补入 `python-multipart==0.0.32` 和 `requests==2.34.2`，README改为先创建`.venv`并安装 `.[dev]`；系统Python不再作为测试前提；
- [x] 锁定环境全量测试245通过、2跳过；30图正式基线仍为255房间、115门、95窗、30/30 Viewer；
- [x] 消融“无门窗证据的大虚拟缺口不封房”：1500/2000mm档逐图房间绝对误差只改善1，却使门确认减少1；1000mm档还使桌面房间50降至47，全部拒绝；
- [ ] 继续逐图解决房间误差；虚拟缺口是否参与闭合必须使用更强的局部拓扑证据，不能只按宽度或是否有门窗证据全局决定。

## 50. 2026-09-09 整圈无墙支撑房间过滤

- [x] 对过滤后的30图交付房间计算整圈边界在原始墙Mask上的支撑，区别于仅检查矩形补边的第47节；
- [x] 冻结保守门槛：面积不超过3㎡且至少58%周长没有真实墙墨迹；30图命中2个，二者与全部真值房间最佳IoU均为0；
- [x] 桌面9图只命中 `fp-108` 一个0.15㎡碎片；`fp-105/106/107/109`大型兜底外壳均由3㎡面积保护保留；
- [x] 过滤继续在门窗仲裁完成后执行，写入 `automatic-room-evidence.json.unsupportedWallRoomIds`；
- [x] ResPlan房间由255降至253，门115、窗95、30/30 Viewer保持；桌面房间由50降至49，门23、窗3、9/9 Viewer保持；
- [ ] 剩余房间误差不再满足安全过滤证据；下一步转向缺失房间恢复和局部房间合并，不继续降低58%或扩大3㎡上限。

## 51. 2026-09-09 墙角微缝闭合校准

- [x] 针对欠分割严重样本消融房间Barrier闭合半径0.75/0.9/1.0/1.25倍墙厚；0.9是唯一同时改善公共真值和桌面门召回的保守档；
- [x] 默认闭合半径由0.75调至0.9倍墙厚；仍远小于300mm物理开口，不取消虚拟门窗缺口表达；
- [x] 新增确定性墙角微缝测试：10px墙宽、28px端点断裂在0.75档泄漏，0.9档可闭合；
- [x] ResPlan逐图房间计数绝对误差由60降至49，真值覆盖房间由179增至190，IoU低于0.1的预测房间由60降至54；
- [x] ResPlan当前258房间、121门、95窗、30/30 Viewer；新增6门均落在真值附近；
- [x] 桌面当前50房间、25门、3窗、9/9 Viewer；新增 `fp-101`、`fp-108` 两门均为可见门弧；
- [ ] `resplan-00008`仍为8/4房间，说明其欠分割不是普通墙角微缝；下一步按该图追踪缺失墙段，不再继续提高全局闭合半径。

## 52. 2026-09-09 项目.env与Tuzi探针去重

- [x] 从用户明确指定的桌面密钥文件生成项目根`.env`；只写入本机且已被Git忽略，日志和工件不包含密钥内容；
- [x] 新增无密钥的`.env.example`，配置Tuzi Base URL和主/复核模型；包初始化及独立image-2脚本使用`python-dotenv`自动读取项目`.env`，且不覆盖显式进程环境变量；
- [x] `start-dev.ps1`优先使用项目`.venv`解释器，系统Python仅作回退，避免宿主运行时切换导致启动失败；
- [x] 主/复核模型ID相同时只做一次正常探针；首次为瞬时不可用时最多重试一次，并让两个角色共享最终能力结论；
- [x] 本机验证`.env`自动加载且脱敏输出；首次在线两次调用出现503后成功一次，随后重试仍为HTTP 503，当前阻塞位于Tuzi上游而非密钥加载；
- [ ] Tuzi恢复后重新执行真实VlmDraft和image-2整批Smoke；503期间继续自动走CV/结构全景，不阻断上传链。

## 53. 2026-09-10 Tuzi视觉模型切换与真实VlmDraft恢复

- [x] Tuzi `/v1/models` 当前Key返回795个模型；按图片＋严格JSON探针实测替代端点，不凭模型名自动选择；
- [x] `gemini-3.5-flash-lite`和`gemini-3.7-flash`通过探针；`gemini-3/3.6/3.8-flash`、`gemini-2.5-flash`、`gemini-3.1-pro`、`gpt-4o`未通过当前图片探针，`gemini-2.5-pro`返回403；
- [x] 当前本机`.env`切换为主模型`gemini-3.7-flash`、复核模型`gemini-3.5-flash-lite`；`.env.example`同步非敏感默认值；
- [x] Provider扁平Schema为所有归一化坐标补齐0～1的 `minimum/maximum`；明显越界继续由本地契约拒绝；
- [x] 解析器只规范化“末点与首点完全相同”的显式多边形闭合，不修改自交、多边形顺序或越界几何；
- [x] 视觉失败阶段保留脱敏Provider证据，`parse_one.py`导出attemptCount、HTTP摘要、请求ID、耗时和响应Hash，不保存Key、图片或模型自由文本；
- [x] `resplan-00008`真实ASSISTED解析成功：VLM草案6房14开口，融合6房1开口，QA accepted；完整链状态ready、6房、3个自动窗并生成Viewer，相比CV 4房恢复2房；
- [x] 主模型在有界重试后仍失败且不是内容安全拦截时，自动切换已配置复核模型；缓存键同时包含主/复核模型，成功证据记录实际请求模型及主模型失败摘要；
- [x] 用3.7主模型完成3图真实VlmDraft小批回归：`00001`真值9/VLM8/融合8、`00059`真值10/VLM8/融合6、`00071`真值9/VLM8/融合8；3/3均返回合法草案，`00059`因2个重叠房间标为needsReview但上传主链仍自动使用最佳预览，不产生人工放行门；
- [ ] 房间精度下一步先处理`00059`重叠冲突和三个样本共同少分1～2房的问题；门窗草案虽有14～19个，但大部分仍被WallEdge/物理缺口证据拒绝，不通过放宽发布门解决；
- [ ] image-2整批Smoke仍未执行，本节只恢复视觉识别，不代表AI装修生图稳定。

## 54. 2026-09-10 image-2单房与本地超分在线Smoke

- [x] 使用项目`.env`和`gpt-image-2`真实调用`/v1/images/edits`成功；返回图片实际为`2048×1024`、2.93MB，证明当前`quality=2k`不等于4K源图；
- [x] 真实输出自动交给现有Real-ESRGAN瓦片链，生成`6144×3072`主图及`4096×2048/2048×1024/1024×512`代理，左右接缝MAE为0；
- [x] 修复`parse_one.py --out`和`upscale_ai_panorama.py --output-dir`在新目录下不会自动创建父目录的问题；不新增依赖或超分实现；
- [x] 全量回归保持`254 passed, 2 skipped`；
- [x] 从真实`POST /api/v1/pipeline/upload`完成`resplan-00072`多房间整批Smoke：真值6房、VLM/融合5房；识别出的5/5房间全部image-2成功、5/5全部生成6K超分，无AI或超分失败，并自动汇总最终PanoManifest；证明功能链完整，但少识别1房仍属于识别精度欠账；
- [x] 多房间实跑暴露任务长期停在60%的可观测性问题；复用现有进度回调，在每房完成后写入`panorama-room-N-of-M`及60～94%的单调进度，不改变失败回退和渲染实现；
- [x] 修复本地Viewer启动后链接拒绝连接：Vite旧启动方式只监听IPv6 `::1`，而产物链接使用`127.0.0.1`；启动脚本现显式绑定IPv4并等待真实5173监听PID，实测Viewer和Manifest均返回HTTP 200；
- [ ] image-2当前只验证一次5房全成功，不宣传上游长期稳定；后续普通回归不重复付费整批调用，遇到真实失败时继续验证备用/结构回退证据。

## 55. 2026-09-10 门窗渲染尺度与门扇修复

- [x] 对五房在线产物逐项核查LayoutRevision：确认门宽存在433/563mm、窗宽大量301～543mm，结构ERP按原值开洞导致全景出现不合逻辑的细缝；
- [x] 识别工件继续保留原始测量值；渲染参考单独采用门净宽至少800mm、窗净宽至少900mm，并把开口扩展到相邻共线墙段，不以展示修正污染识别评测；
- [x] 删除image-2提示中误伤真实门的“禁止handles/door-shaped panels”表述；明确每个已确认门必须安装门框、门扇和可见把手，每个窗必须安装窗框和玻璃，禁止留下空洞或细缝；
- [x] 用`resplan-00072`卧室真实重生成验证：门框、门扇、把手和正常尺度窗均出现，结构代理QA accepted；本地超分生成6144×3072，接缝MAE为0；
- [x] 人工目视复核否决首版：1371mm开口被生成为接近90°伸入房间的超宽单门叶，证明低频结构QA不能代替门窗语义QA；首版不作为验收产物；
- [x] 第二次提示修订把门叶统一闭合，真实重生成v2已消除超宽外开门；但1371mm与800mm仍造成两门一大一小，v2只作为拒绝证据，不是最终验收产物；
- [x] 再次对照原户型确认卧室确有入口门和套卫门两扇；一大一小来自VLM入口宽1371mm与CV套卫门宽563mm的测量差，不是ERP重复门；门型尚未分类时，渲染净宽最终钳制为800～900mm，原始识别宽度继续留档；
- [x] 真实重生成v3虽把入口门900mm、套卫门800mm生成为闭合门，但image-2把跨ERP接缝的一扇门左右两半分别补成完整门，球面实际出现第三扇；v3判废，不能把“展开图接缝”当成模型会自动理解的前提；
- [x] 结构ERP新增自动接缝朝向：按相机到全部开口的方位和可视半宽，从720个候选方向选择离开口最远的实墙作为接缝；确定性测试锁定单门位于0°时接缝应落在180°；
- [x] 真实重生成v4目视确认画面只有两扇正常闭合门、两扇窗，左右端均为连续实墙；结构QA accepted，6144×3072超分接缝MAE为0，并生成独立Viewer验收地址；
- [x] 回归保持`254 passed, 2 skipped`；
- [ ] 当前只重生成一间高暴露卧室验证修复，不重复付费重跑旧五房；后续新上传自动使用修正逻辑，若还出现空门洞再增加门窗语义QA而不是继续叠提示词。

## 56. 2026-09-11 视觉成功草案跨进程缓存

- [x] 定位`resplan-00059`跨请求波动的直接稳定性问题：一次真实VLM得到10房且融合10房，随后上传服务遇Tuzi瞬时失败退回CV只剩7房；旧缓存仅在调用进程内有效；
- [x] 复用现有`VisionResultCache`增加本机磁盘持久化，默认写入`PANO_DATA_ROOT/_vision_cache`，保持6小时TTL与256项上限，不引入Redis或新服务；
- [x] 仅持久化通过`VlmDraft`严格契约的规范化草案、脱敏证据和Hash；读取时重新验证契约与`normalizedDraftSha256`，损坏、篡改或过期条目自动删除；
- [x] 写入使用同目录临时文件加原子替换，同图、Provider、主/备用模型组合、Prompt、Schema和预处理版本任一变化都会生成不同任务键；
- [x] 新增独立缓存实例恢复与损坏文件拒绝测试；定向回归`20 passed`；
- [x] 真实`resplan-00059`首进程在线得到8房/17开口并写盘，第二个独立Python进程以同一任务键直接`cacheHit=true`恢复；测试改为注入纯内存缓存，不再清理真实运行缓存；
- [ ] 本层只解决同机跨进程/重启复用，不实现跨机器共享与分布式in-flight等待；后者继续归正式云任务系统，不阻塞当前本地交付。

## 57. 2026-09-11 小房邻接重叠融合修复

- [x] 用持久缓存固定`resplan-00059`当前9房草案，稳定复现旧融合把相邻大卧室和小卫生间同时标为`ROOM_OVERLAP_CONFLICT`，最终只剩7房；
- [x] 不放宽通用重叠门槛；仅当小房面积不超过大房40%、重叠不超过大房30%，且大房减去小房后仍是单个无孔合法多边形时，保留小房并裁掉大房的冲突带；
- [x] 面积接近、同类型、相减后分裂或产生孔洞的冲突继续进入复核；原有“大面积异类重叠双拒绝”测试保持；
- [x] 新增小房跨相邻边界的确定性测试；`resplan-00059`融合由7闭合/2复核变为9闭合/0复核，解析状态由`needsReview`变为`accepted`；
- [x] 后端重启后经真实上传API回归：视觉阶段明确`rooms=9, openings=16, cache=hit`，融合`rooms_closed=9`，任务100% DONE并生成9房结构Viewer；未调用image-2；
- [ ] 公共真值仍为10房，当前固定VLM草案只提供9房；本修复只消除融合双杀，剩余1房必须从缺失房间恢复解决，不能由重叠裁剪臆造。

## 58. 2026-09-11 GPT Image 2.5 原生4K到8K案例

- [x] Tuzi `/v1/models`确认正式模型ID为`gpt-image-2.5`；定价页未登录会跳转登录，因此不依赖页面文案猜参数；
- [x] 现有单房脚本增加`--model/--quality/--size`参数，真实调用`gpt-image-2.5 + quality=4k + size=4096x2048`成功；
- [x] 网关实际返回`3824×1904` PNG、8.93MB，并非请求标称的严格`4096×2048`或严格2:1；超分入口对5%以内比例漂移自动规范化；
- [x] Real-ESRGAN环绕瓦片链增加保留原始输入和自定义输出尺寸；本例不再先降至2K，经50个重叠瓦片生成`8192×4096`主图及4K/2K/1K代理；
- [x] 输出接缝MAE为`0.0000`；结构代理QA accepted，参考边缘召回`0.9174`、候选精度`0.5971`、长线数量22/22；
- [x] 独立Viewer与8K/4K/2K/1K资源已生成于`verify-image25-4k-case`；用户目视确认细腻度并决定将2.5作为主力模型。

## 59. 2026-09-11 GPT Image 2.5 生产默认与洁净墙面约束

- [x] 生产默认切换为`gpt-image-2.5 / quality=4k / size=4096x2048`；`.env.example`同步，三项均可用环境变量覆盖回退；
- [x] 共用装修Prompt要求均匀纯色哑光乳胶漆，禁止微水泥、艺术漆、石纹、斑驳、污渍、补丁和云状渐变；要求单一一致日光方向与均匀曝光，禁止墙洗灯、不明光斑和脏阴影；
- [x] 生产链保留模型实际返回的近2:1原生分辨率，不再先压回2K；比例漂移超过5%直接拒绝，5%以内规范化为严格2:1后进行环绕超分；
- [x] 默认超分主图升级为`8192×4096`，继续生成4K/2K/1K代理；若超分失败，Manifest记录AI原图真实尺寸并另建2K代理，不再错误声明分辨率；
- [x] 新增默认值及环境覆盖测试；不新增模型路由、依赖或服务。

## 60. 2026-09-11 涂料墙低频斑驳发布门

- [x] 在现有结构QA内复用参考ERP的中性墙面区域，计算候选图局部与大尺度模糊之间的低频不均匀P90，不新增模型或外部调用；
- [x] 仅对客厅、客餐厅、卧室、书房和其他涂料墙房型启用；厨房、卫生间等橱柜/瓷砖主导房型跳过，候选墙面覆盖不足10%也跳过；
- [x] 冻结首版保守门槛P90 `30`：当前2.5脏墙案例为`32.54`并触发`WALL_SURFACE_UNEVEN`；旧客厅、奶油风和修正卧室约`26.5`且保持accepted；
- [x] 洁净度结论写入每房`ai-structure-qa.json.wallCleanliness`；命中时与结构漂移一样不发布AI图，自动保留确定性结构预览；
- [x] 新增合成云状斑驳回归测试；该门只覆盖明显低频脏斑，不宣称识别所有材质或真实光照语义。

用户需要解决的外部卡点：

- [ ] 若要落地“程序化家具布局”，需要提供或确认一套可商用 GLB 家具资产包及许可证；否则主链继续由 image-2 自动生成家具视觉，不承诺家具三维实体可编辑；
- [ ] 部署24小时客户分享前，需要确定云厂商/项目并提供部署权限或由用户完成部署；短期无需购买独立域名；
- [ ] 若 Tuzi 后续返回余额、配额或账号级模型不可用，需要用户在中转站侧处理。当前 `/v1/models` 为200，配置的 `gemini-3.5-flash` 可见，Key和模型环境变量均已正确配置，不是当前阻塞。

## 20. 2026-09-01 异步任务阶段进度

- [x] 后端上传任务在解析、几何、结构预览、全景完成和终态阶段回报单调进度；
- [x] 进度回调异常被隔离，不会把可交付预览变成失败任务；
- [x] 上传页轮询并显示百分比与当前阶段，用户无需猜测任务是否仍在运行；
- [ ] 任务状态仍是单进程内存，生产环境的持久化、租约、取消和重启恢复继续归P0-10/P0-11。

明确不做：

- 不把第三方开源权重引入生产；
- 不解析VLM自由文本落库；
- 不把VLM自陈confidence当发布门；
- 不在浏览器调用Tuzi；
- 不自动选择模型列表中的任意替代模型；
- 不在未过留出集时宣传“自动识别率90%”。
