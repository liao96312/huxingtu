# ADR-0002：GPT Image 2 与目标 Viewer 基线

- 状态：Accepted
- 日期：2026-08-24
- 修订：ADR-0001 的 AI Provider 选择

## 目标

最终成品以用户指定的 第三方全景分享页 为黑盒交互与画质基线，但不得依赖其代码、域名、数据或私有协议。

实测基线：

- 单张全景为 6144×3072 JPEG、严格 2:1，样例约 10.1MB；
- 2 个房间共 8 个视角（客餐厅 5、厨房 3）；
- 桌面和 390×844 移动端均为全屏 Canvas、底部横向缩略图、当前项勾选和右上分享；
- 支持拖拽环视和视角切换，切换时显示加载状态。

## 决策

P0 AI 主路径固定为：

```text
Three.js 结构 ERP
  -> GPT Image 2 Image Edits（3840×1920, high）
  -> 结构/接缝/极区 QA
  -> wrap-aware 超分（6144×3072）
  -> 2K/1K 代理图 + 200px 缩略图
  -> 多视角 Viewer 与分享 Manifest
```

- 使用官方模型 ID `gpt-image-2`；生产评测固定快照 `gpt-image-2-2026-04-21`。
- API Key 只存在于服务端 Secret，不进入浏览器、日志、任务 Payload 或对象存储。
- 使用 Image Edits，并将结构 ERP 作为第一张输入；可见家具参考图是后续有限输入。
- 不传 `input_fidelity`；GPT Image 2 对输入图自动使用高保真处理。
- GPT Image 2 最长边为 3840px，不能直接请求 6144×3072；P0 生成尺寸固定为 3840×1920。
- GPT Image 2 的 2K 以上输出仍属实验能力，必须通过独立质量门，不能仅凭请求成功发布。
- ComfyUI 只在 GPT Image 2 Spike 无法达到结构与接缝门槛时重新评估，不并行建设。

## 官方依据

- [GPT Image 2 模型页](https://developers.openai.com/api/docs/models/gpt-image-2)
- [OpenAI Image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
