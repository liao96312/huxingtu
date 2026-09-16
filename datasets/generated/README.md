# 户型图视觉方向样张

这些图片用于确认 360°室内设计的视觉方向和接缝检测方法，不是 GPT Image 2 Spike 结果，也不能证明户型几何已被正确保持。

## 输入

| 字段 | 值 |
|---|---|
| Sample | `fp-001` |
| 文件 | `../source/fp-001.png` |
| SHA256 | `1eb6c0bc9f5ede557d1b39ab596b3482d0699256a2df94a4ab00bdcc2f6d7966` |
| 角色 | 户型空间关系和房间功能参考，不是编辑目标 |

## 结果

| 版本 | 文件 | 尺寸 | SHA256 | Edge MAE | Edge P95 | 结论 |
|---|---|---:|---|---:|---:|---|
| v1 | `fp-001-living-visual-v1.png` | 1774×887 | `e5ea68cd010620effc5b407baf917ebaf9934596509fca208e377f07c5bf21e0` | 11.515 | 57 | 2:1 成立，接缝不合格 |
| v2 | `fp-001-living-visual-v2-seam.png` | 1774×887 | `ca5bb09fad5fba335d56af578d0e52fe2362b5fccecbaf9bdda1f98e89361784` | 8.324 | 33 | 接缝改善，仍只作为视觉基线 |

v2 相对 v1：Edge MAE 降低约 27.7%，Edge P95 降低约 42.1%。两版黑像素比例均低于 0.1%。

## v1 Prompt

```text
Use case: photorealistic-natural
Asset type: visual direction sample for a 360-degree interior panorama viewer
Input images: Image 1 is a floor-plan reference for room program, adjacency, and approximate proportions; it is not an edit target and must not appear in the output.
Primary request: generate a premium photorealistic interior panorama viewed from the center of the living and dining area, following Image 1's spatial logic.
Scene/backdrop: compact two-bedroom apartment; living room on the larger west/south side, dining area toward the east near the kitchen, bedroom and bathroom doors toward the north/east circulation zone.
Style/medium: high-end architectural interior photography, warm contemporary cream-and-natural-wood design, realistic fabric, stone, wood grain, glass and metal.
Composition/framing: true 2:1 equirectangular 360x180 panorama, complete ceiling and floor coverage, horizon centered, camera height about 1.2 meters, seamless left/right wrap, balanced panoramic exposure, straight vertical architecture.
Lighting/mood: soft natural daylight with warm indirect interior lighting, realistic shadows, calm premium residential atmosphere.
Constraints: preserve plausible room adjacency and openings from the reference; the left and right image boundaries must join seamlessly; no people; no text; no labels; no floor-plan overlay; no logos; no watermark.
Avoid: ordinary rectilinear wide photo, circular fisheye frame, duplicated furniture, warped doors, broken walls, floating objects, extra rooms or openings not implied by the plan.
```

## v2 Prompt

```text
Use case: precise-object-edit
Asset type: seam-safe 360-degree panorama visual QA sample
Input images: Image 1 is the edit target.
Primary request: change only the narrow left and right boundary regions so the image becomes a seamless cylindrical equirectangular panorama when the right edge wraps directly into the left edge.
Required boundary behavior: ceiling color and texture, wall surfaces, built-in wood cabinetry, floorboards, lighting, perspective lines, and exposure must continue naturally across the 0°/360° join with no visible vertical seam.
Constraints: preserve the exact 2:1 equirectangular composition, camera height, horizon, room geometry, furniture, doors, windows, materials, lighting, color palette, and all central image content; do not crop, rotate, redesign, add, remove, duplicate, or move objects; no text; no watermark.
Avoid: ordinary rectilinear photo, fisheye circle, mirrored boundary objects, repeated furniture, new openings, warped architecture, tonal jump at the wrap boundary.
```

## 生成方式与限制

- 使用内置 `imagegen`，不是用户的 GPT Image 2 API。
- 输出已复制到项目，默认生成目录中的原文件未删除。
- 当前样张低于最终 3840×1920 中间工件和 6144×3072 发布工件要求。
- 后续 P0-08 必须用 Three.js 结构 ERP 作为第一输入，独立验证墙、门、窗和跨视角一致性。
