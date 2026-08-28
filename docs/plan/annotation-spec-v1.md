# 户型图真值标注规范 v1（已冻结）

> 状态：**已冻结**（2026-08-27）
>
> 本规范约束全部真值标注的产出格式与口径，与 `floorplan-recognition-v1.md` §2 验收指标一一对应。
> 冻结后如需修改口径，必须走 ADR 流程并同步更新已产出的标注，禁止悄悄改。

## 1. 坐标与文件约定

- 坐标一律为**像素坐标**：原点左上、y 向下，使用 PNG 原始尺寸，不做缩放、不裁剪。
- mm 换算一律由 pipeline 用比例尺完成，标注文件不写 mm（比例尺数值除外）。
- 每张图一个标注文件：`datasets/annotations/<sample-id>.annotation.json`。
- 每个标注文件必须包含 `schemaVersion`、`sampleId`、`imageSha256`（防错图），及标注者与时间。
- 产出即入库、即算哈希备份，不允许只存在临时目录。

## 2. 冻结规则

### R-01 垭口分房规则

- 隔断上有**门扇或门弧**（`door`）→ 两侧视为独立房间。
- 隔断上是**开放式垭口**（`doorway`，无门扇、无门弧）→ 两侧视为同一房间，不新增房间。
- `doorway` 类型**仍必须标注出来**，但它不参与房间分割。
- 任何例外必须在 `notes` 中写明，由评审确认。

### R-02 墙中心线口径

- 房间多边形顶点一律落在**内墙中心线**上（墙带中线），不是墙带边缘。
- 建筑外轮廓沿外墙中心线走。
- 无实体墙的边界（如阳台玻璃栏板）以图面视觉中线为准，并在 `notes` 标注该边。

## 3. 标注对象

### 3.1 开口（T1：全部样本）

每个开口标**两个像素点**（沿墙缺口的起点/终点）+ 类型：

| 字段 | 取值 |
|---|---|
| `type` | `door` / `window` / `doorway` |
| `pixelStart` / `pixelEnd` | 缺口两端像素坐标，顺序无要求 |
| `evidence`（可选） | `doorArc` / `doorLeaf` / `parallelLines` / `none` |

判定：门弧或门扇 → `door`；平行细线跨缺口 → `window`；无证据且宽度在门宽区间 → `doorway`。

### 3.2 房间（T2：先做 3–5 张，逐步补齐）

- 每个房间一个闭合多边形（≥3 顶点），顶点沿内墙中心线（R-02）。
- 类型枚举：`bedroom` / `living` / `dining` / `livingDining` / `kitchen` / `bathroom` / `balcony` / `study` / `other`。
- `livingDining`（客餐厅）：客厅与餐厅连成一个开放空间、中间无门/无墙隔断时，标成**一个**客餐厅多边形；中间有门或有墙才拆成 `living` + `dining`。
- 房间分割遵循 R-01。
- 除 `other` 外，类型不明的房间宁可标 `other` 也不猜，猜测会污染语义准确率。

### 3.3 比例尺（T3：每张至少 0–2 条）

- 图上有尺寸数字时：标尺寸线两端像素点 + 毫米数值（如 `3600`）。
- 图上没有尺寸数字时：**留空**，由 pipeline 用「内隔墙厚 ≈ 110mm」自举，并在结果中标记 `scaleBootstrap`，该图比例尺指标按 `notEvaluated` 计。
- 禁止用「目测房间大小」反推比例尺——这会造成循环验证。

## 4. 标注文件 JSON 格式

```json
{
  "schemaVersion": "0.1.0",
  "sampleId": "fp-001",
  "imageSha256": "<sha256 of source png>",
  "annotator": "<name>",
  "annotatedAt": "2026-08-27T00:00:00+08:00",
  "notes": "",
  "openings": [
    {
      "id": "op_001",
      "type": "door",
      "pixelStart": {"x": 433, "y": 203},
      "pixelEnd": {"x": 433, "y": 232},
      "evidence": "doorArc"
    }
  ],
  "rooms": [
    {
      "id": "room_001",
      "type": "bedroom",
      "polygon": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 80}]
    }
  ],
  "scales": [
    {
      "id": "scale_001",
      "pixelStart": {"x": 10, "y": 700},
      "pixelEnd": {"x": 410, "y": 700},
      "lengthMm": 3600
    }
  ]
}
```

## 5. 指标映射

| 验收指标（v1 §2） | 所需标注 | 未标注时 |
|---|---|---|
| Room mIoU | `rooms[].polygon` | `notEvaluated` |
| 房间数完全正确率 | `rooms` 数量（按 R-01 口径） | `notEvaluated` |
| 邻接边 F1 | `rooms` 多边形推导 | `notEvaluated` |
| 门窗 F1 | `openings[].type` | `notEvaluated` |
| 比例尺相对误差 | `scales[]` | 自举 + `notEvaluated` |
| 墙中心线误差 | 暂不要求（T4） | `notEvaluated` |

> 任何指标在真值缺失时一律标 `notEvaluated`，禁止用算法自身输出当真值（v1 诊断 D-05）。
