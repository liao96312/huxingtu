import { useEffect, useMemo, useState } from "react";

type Candidate = {
  id: string;
  kind: "door" | "window";
  wallEdgeId: string;
  width: number;
  confidence: number;
  method: string;
  pixelStart: { x: number; y: number };
  pixelEnd: { x: number; y: number };
  sideARoomId: string | null;
  sideBRoomId: string | null;
};

type CandidateSet = { candidates: Candidate[] };
type Decision = "confirmed" | "rejected";

export default function OpeningReview({ projectId }: { projectId: string }) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [selectedId, setSelectedId] = useState("");
  const [message, setMessage] = useState("正在加载门窗候选…");

  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}/opening-candidates`)
      .then((response) => {
        if (!response.ok) throw new Error(`候选加载失败（${response.status}）`);
        return response.json() as Promise<CandidateSet>;
      })
      .then((value) => {
        setCandidates(value.candidates);
        setSelectedId(value.candidates[0]?.id ?? "");
        setMessage("");
      })
      .catch((error: Error) => setMessage(error.message));
  }, [projectId]);

  const reviewedCount = Object.keys(decisions).length;
  const selected = useMemo(() => candidates.find((item) => item.id === selectedId), [candidates, selectedId]);

  const submit = async () => {
    setMessage("正在生成已确认 Revision…");
    const response = await fetch(`/api/v1/projects/${projectId}/opening-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decisions: candidates.map((candidate) => ({ candidateId: candidate.id, decision: decisions[candidate.id] })),
      }),
    });
    const result = await response.json();
    setMessage(response.ok
      ? `完成：${result.confirmedCount} 个确认，${result.rejectedCount} 个驳回，已生成 ${result.layoutRevisionId}`
      : result.detail ?? "提交失败");
  };

  const decide = (decision: Decision) => {
    if (!selected) return;
    setDecisions((current) => ({ ...current, [selected.id]: decision }));
    const next = candidates.find((item) => !decisions[item.id] && item.id !== selected.id);
    if (next) setSelectedId(next.id);
  };

  return (
    <main className="review-shell">
      <section className="review-canvas" aria-label="户型图门窗候选">
        <img src="/review/fp-001.png" alt="fp-001 原始户型图" />
        <svg viewBox="0 0 621 450" role="img" aria-label="门窗候选位置">
          {candidates.map((candidate) => (
            <line
              key={candidate.id}
              className={`${candidate.kind} ${selectedId === candidate.id ? "selected" : ""} ${decisions[candidate.id] ?? "pending"}`}
              x1={candidate.pixelStart.x}
              y1={candidate.pixelStart.y}
              x2={candidate.pixelEnd.x}
              y2={candidate.pixelEnd.y}
              onClick={() => setSelectedId(candidate.id)}
            />
          ))}
        </svg>
      </section>

      <aside className="review-panel">
        <header>
          <h1>门窗候选确认</h1>
          <p>{reviewedCount}/{candidates.length} 已处理 · 点击图中线段或列表切换</p>
        </header>
        <ol className="candidate-list">
          {candidates.map((candidate, index) => (
            <li key={candidate.id}>
              <button className={selectedId === candidate.id ? "active" : ""} onClick={() => setSelectedId(candidate.id)}>
                <b>{index + 1}. {candidate.kind === "door" ? "门" : "窗"}</b>
                <span>{Math.round(candidate.width)}mm · 置信度 {Math.round(candidate.confidence * 100)}%</span>
                <em>{decisions[candidate.id] === "confirmed" ? "已确认" : decisions[candidate.id] === "rejected" ? "已驳回" : "待处理"}</em>
              </button>
            </li>
          ))}
        </ol>
        {selected && (
          <div className="candidate-detail">
            <p>墙：{selected.wallEdgeId}</p>
            <p>两侧：{selected.sideARoomId ?? "室外"} ↔ {selected.sideBRoomId ?? "室外"}</p>
            <p>依据：{selected.method}</p>
            <div>
              <button className="reject" type="button" onClick={() => decide("rejected")}>驳回</button>
              <button className="confirm" type="button" onClick={() => decide("confirmed")}>确认</button>
            </div>
          </div>
        )}
        <button className="submit-review" type="button" disabled={reviewedCount !== candidates.length} onClick={submit}>
          生成已确认 Revision
        </button>
        {message && <p className="review-message" role="status">{message}</p>}
      </aside>
    </main>
  );
}
