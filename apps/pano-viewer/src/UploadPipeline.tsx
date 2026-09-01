import { useState } from "react";

type PipelineResult = {
  projectId: string;
  pipelineStatus: string;
  progress?: number;
  stage?: string;
  parseStatus: string;
  message?: string;
  warning?: string;
  viewerUrl?: string | null;
  roomCount?: number;
  aiGeneratedCount?: number;
  aiFailedRooms?: string[];
  superresGeneratedCount?: number;
  superresFailedRooms?: string[];
  stages: { stage: string; status: string; detail: string }[];
};

export default function UploadPipeline() {
  const [file, setFile] = useState<File | null>(null);
  const [projectId, setProjectId] = useState("");
  const [externalVision, setExternalVision] = useState(true);
  const [generateAi, setGenerateAi] = useState(true);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [processingStage, setProcessingStage] = useState("等待任务");
  const [error, setError] = useState("");
  const [result, setResult] = useState<PipelineResult | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setProgress(0);
    setProcessingStage("提交任务");
    setError("");
    setResult(null);
    const body = new FormData();
    body.append("file", file);
    if (projectId.trim()) body.append("projectId", projectId.trim());
    body.append("allowExternalVision", String(externalVision));
    body.append("generateAi", String(generateAi));
    try {
      const response = await fetch("/api/v1/pipeline/upload", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? `HTTP ${response.status}`);
      if (response.status === 202 && payload.pollUrl) {
        setProgress(payload.progress ?? 0);
        setProcessingStage(payload.stage ?? "排队中");
        for (;;) {
          await new Promise((resolve) => window.setTimeout(resolve, 1200));
          const poll = await fetch(payload.pollUrl);
          const task = await poll.json();
          if (!poll.ok) throw new Error(task.detail ?? `任务查询 HTTP ${poll.status}`);
          setProgress(task.progress ?? 0);
          setProcessingStage(task.stage ?? "处理中");
          if (["DONE", "NEEDS_REVIEW", "FAILED"].includes(task.status)) {
            if (task.result) setResult(task.result as PipelineResult);
            else throw new Error(task.error ?? "流水线失败");
            break;
          }
        }
      } else {
        setResult(payload as PipelineResult);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传处理失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="pipeline-page">
      <form className="pipeline-card" onSubmit={submit}>
        <a className="pipeline-back" href="/">← 返回全景播放器</a>
        <h1>户型图 → 360°全景测试</h1>
        <p>上传后自动执行视觉识别、几何融合、3D预览和ERP全景。未过质量门的结果会明确标成“预览/待复核”。</p>

        <label className="file-drop">
          <span>{file ? file.name : "选择 PNG / JPG / WebP 户型图"}</span>
          <input accept="image/png,image/jpeg,image/webp" type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </label>

        <label>
          项目ID（可选）
          <input value={projectId} pattern="[a-zA-Z0-9_-]{1,128}" placeholder="自动生成" onChange={(event) => setProjectId(event.target.value)} />
        </label>
        <label className="pipeline-check">
          <input type="checkbox" checked={externalVision} onChange={(event) => setExternalVision(event.target.checked)} />
          允许将本图发送给 Tuzi 视觉模型进行结构识别
        </label>
        <label className="pipeline-check">
          <input type="checkbox" checked={generateAi} onChange={(event) => setGenerateAi(event.target.checked)} />
          对所有闭合房间调用 image-2 生成装修全景（默认开启，会按房间计费）
        </label>
        <button className="pipeline-submit" type="submit" disabled={!file || busy}>
          {busy ? `识别和生成中 ${progress}% · ${processingStage}…` : "开始生成全景"}
        </button>

        {error && <div className="pipeline-error" role="alert">{error}</div>}
        {result && (
          <section className="pipeline-result">
            <h2>{result.viewerUrl ? "全景预览已生成" : "识别未达到生成条件"}</h2>
            <p>项目：{result.projectId} · Parse：{result.parseStatus} · 房间：{result.roomCount ?? 0} · AI成功：{result.aiGeneratedCount ?? 0} · 超分成功：{result.superresGeneratedCount ?? 0}</p>
            {(result.message || result.warning) && <p>{result.message ?? result.warning}</p>}
            <ol>{result.stages.map((stage) => <li key={stage.stage}><b>{stage.stage}</b>：{stage.status} — {stage.detail}</li>)}</ol>
            {result.viewerUrl && <a className="pipeline-open" href={result.viewerUrl}>打开可拖动360°全景</a>}
          </section>
        )}
      </form>
    </main>
  );
}
