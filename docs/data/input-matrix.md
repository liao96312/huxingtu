# P0 输入与拒绝矩阵

## 当前支持边界

| 输入特征 | P0 决策 | Reason Code | 说明 |
|---|---|---|---|
| 单层、正交、短边 ≥1200px、比例尺可确认 | `accept` | — | 进入自动解析 |
| 单层且结构清楚，但短边 <1200px | `needsReview` | `SHORT_EDGE_BELOW_1200` | 允许 OCR/人工确认比例，不直接作为高置信几何 |
| 缺少可验证比例尺 | `needsReview` | `SCALE_CONFIRMATION_REQUIRED` | 用户输入一条基准尺寸后继续 |
| 包含庭院、车位或完整地块 | `reject` | `SITE_PLAN_INCLUDED` | P0 不解析场地设计 |
| 包含楼梯且声明多楼层 | `reject` | `MULTILEVEL_INPUT` | 成对保留到 P1 数据切片 |
| 明确为二层及以上 | `reject` | `UPPER_FLOOR_PLAN` | P0 仅处理单层 |

## 首批样本结论

| Sample | 图像 | 预期门禁 | 用途 |
|---|---:|---|---|
| `fp-001` | 621×450 | `needsReview` | 低清单层正交户型；测试比例确认和人工修订入口 |
| `fp-002` | 412×766 | `reject` | 一层场地/车库/楼梯；测试复杂输入正确拒绝 |
| `fp-003` | 430×782 | `reject` | 二层户型；与 `fp-002` 配对用于 P1 多楼层 |

这些样本属于 PoC 数据，不进入未来独立测试集，也未获准用于模型训练。

`fp-001` 已使用 4 段横向和 2 段纵向尺寸完成比例确认，结果为 `28.9231mm/px`、最大相对残差约 2.44%；详见 [`fp-001-scale-calibration.md`](./fp-001-scale-calibration.md)。分辨率门禁仍未解除。
