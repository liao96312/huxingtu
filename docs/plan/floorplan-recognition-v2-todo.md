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

### [ ] T00 冻结实现范围和数据谱系

依赖：无。

交付物：

- 审计 `backend/pano_platform/floorplan/`、`datasets/source/desktop-9/`、`datasets/debug/desktop9/`、`docs/plan/` 的来源与授权；
- 把代码/Schema/小型证据与原图、密钥、模型权重分开；
- 每张图登记 `sampleId/imageSha256/sourceGrant/split`；
- 冻结训练、验证、留出测试切片；
- 更新数据Manifest与删除/保留策略。

DoD：Git中无来源不明文件；图片可由SHA定位授权；测试集与调参集互斥；未授权图片不会发送Tuzi。

测试：DatasetManifest Schema、重复Hash检测、切片互斥测试。

### [ ] T01 实现Tuzi模型发现与能力探针

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

### [ ] T02 冻结VLM输入/输出契约

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

### [ ] T03 实现Tuzi Responses Adapter

依赖：T01、T02。

目标文件：

```text
backend/pano_platform/vision/provider.py
backend/pano_platform/vision/tuzi_responses.py
tests/test_tuzi_responses_adapter.py
```

任务：实现Provider Protocol；构造 `/v1/responses`；使用严格JSON Schema；解析结构化结果；记录请求ID/实际模型/usage/延迟/响应Hash；有限重试；禁止从Markdown猜JSON。

DoD：Mock测试覆盖成功、429、5xx、超时、审核、模型不存在、截断和Schema错误；真实Smoke仅本地Key存在时运行，CI默认跳过。

### [ ] T04 冻结视觉输入和Prompt版本

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

### [ ] T05 实现证据、缓存、幂等与错误信封

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

### [ ] T06 实现F1吸附与F2比例尺/面积自洽

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

### [ ] T07 实现F3开口仲裁与F4房间闭合

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

### [ ] T08 实现parse命令编排

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

### [ ] T09 建立离线基准与Prompt回归

依赖：T08、完整真值。

目标文件：

```text
scripts/benchmark_floorplan.py
tests/fixtures/floorplan-golden/
docs/data/floorplan-v2-benchmark.json
```

任务：户型族隔离切片；分别测CV、Tuzi Draft、融合结果；输出全部P0指标、JSON有效率、延迟、成本、复核率；模型/Prompt变化跑同一冻结集。

DoD：融合留出集达到方案全部门槛，否则保持SHADOW/ASSISTED。

### [ ] T10 完成安全、隐私和成本控制

依赖：T03、T05、T08。

任务：Secret挂载、日志脱敏、第三方处理披露、用户授权、保留删除、缓存级联、预算/日限额、SSRF/超大图防护、失败/成本告警。

DoD：安全测试通过；删除能清理所有视觉工件；超预算任务不入队。

## 4. 发布阶段

### [ ] T11 Shadow运行

依赖：T08–T10。

- 至少30张PoC图同时跑当前结果和v2；
- VLM不影响当前Revision；
- 记录质量、成本、延迟、Schema有效率和漂移。

退出标准：Schema有效率100%，无泄密/错Revision，融合稳定优于纯CV。

### [ ] T12 Assisted发布门

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
- [ ] 对上述两模型完成 `input_image + strict json_schema` 能力探针；
- [ ] 墙、房间、窗真值补足；
- [ ] 样本扩到30张PoC，发布前独立集≥200张。

明确不做：

- 不把第三方开源权重引入生产；
- 不解析VLM自由文本落库；
- 不把VLM自陈confidence当发布门；
- 不在浏览器调用Tuzi；
- 不自动选择模型列表中的任意替代模型；
- 不在未过留出集时宣传“自动识别率90%”。
