"""Generate a browser review page for automatic door/window predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.floorplan.parse import VisionMode, orchestrate  # noqa: E402
from pano_platform.opening_detection import detect_door_arc_proposals  # noqa: E402


COLORS = {
    "confirmed": (40, 190, 40),
    "candidate": (0, 165, 255),
    "rejected": (40, 40, 220),
    "raw": (220, 60, 220),
    "gap": (0, 220, 255),
    "cvArc": (255, 180, 0),
}


def _point(value, width: int, height: int) -> tuple[int, int]:
    if hasattr(value, "x"):
        return round(value.x * width), round(value.y * height)
    return round(value[0]), round(value[1])


def _draw_line(image: np.ndarray, start, end, color, label: str) -> None:
    a, b = tuple(map(round, start)), tuple(map(round, end))
    cv2.line(image, a, b, color, 4, cv2.LINE_AA)
    cv2.circle(image, a, 5, color, -1, cv2.LINE_AA)
    cv2.circle(image, b, 5, color, -1, cv2.LINE_AA)
    middle = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
    cv2.putText(image, label, (middle[0] + 5, middle[1] - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(image, label, (middle[0] + 5, middle[1] - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)


def _draw_arc(image: np.ndarray, center, radius: float, label: str) -> None:
    point = tuple(map(round, center))
    cv2.circle(image, point, round(radius), COLORS["cvArc"], 3, cv2.LINE_AA)
    cv2.putText(image, label, (point[0] + 5, point[1] - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(image, label, (point[0] + 5, point[1] - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.55, COLORS["cvArc"], 2, cv2.LINE_AA)


def render_sample(image_path: Path, output_dir: Path, mode: VisionMode, config, existing: dict | None = None) -> dict:
    payload = image_path.read_bytes()
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    gray_image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"cannot decode {image_path}")
    height, width = image.shape[:2]
    run = orchestrate(
        payload, project_id=image_path.stem,
        vision_mode=VisionMode.OFF if existing is not None else mode,
        vision_config=None if existing is not None else config,
    )
    cv_stage = next((stage for stage in run.stages if stage.stage == "cv_structure"), None)
    vision_stage = next((stage for stage in run.stages if stage.stage == "vision"), None)
    fusion_stage = next((stage for stage in run.stages if stage.stage == "fusion"), None)
    gap_overlay = image.copy()
    arc_overlay = image.copy()
    fusion_overlay = image.copy()

    graph = cv_stage.data.get("wallGraph") if cv_stage is not None else None
    gaps = list(getattr(graph, "gaps", ()))
    predictions = []
    for index, gap in enumerate(gaps, 1):
        _draw_line(gap_overlay, gap.gap_start, gap.gap_end, COLORS["gap"], f"G{index}")
        predictions.append({
            "id": f"G{index}", "type": "unknownOpening", "status": "cvGap",
            "start": list(gap.gap_start), "end": list(gap.gap_end),
            "widthMm": gap.width_px * graph.mm_per_px if graph.mm_per_px > 0 else None,
            "reasonCodes": ["CV_PHYSICAL_GAP_PROPOSAL"],
        })

    if gray_image is None:
        raise ValueError(f"cannot decode grayscale image {image_path}")
    arcs = detect_door_arc_proposals(gray_image)
    for index, arc in enumerate(arcs, 1):
        _draw_arc(arc_overlay, arc.center, arc.radius_px, f"A{index}")
        predictions.append({
            "id": f"A{index}", "type": "door", "status": "cvArc",
            "center": list(arc.center), "radiusPx": arc.radius_px,
            "widthMm": None, "reasonCodes": ["CV_DOOR_ARC_PROPOSAL_ONLY"],
        })

    if existing is not None:
        for item in existing.get("predictions", []):
            if not item["id"].startswith("P"):
                continue
            _draw_line(fusion_overlay, item["start"], item["end"], COLORS.get(item["status"], COLORS["rejected"]), item["id"])
            predictions.append(item)
    elif fusion_stage is not None:
        items = [
            *(fusion_stage.data.get("confirmedOpenings") or ()),
            *(fusion_stage.data.get("openingCandidates") or ()),
            *(fusion_stage.data.get("rejectedOpenings") or ()),
        ]
        for index, item in enumerate(items, 1):
            status = item.status if item.status in COLORS else "rejected"
            _draw_line(fusion_overlay, item.start_px, item.end_px, COLORS[status], f"P{index}")
            predictions.append({
                "id": f"P{index}", "type": item.type, "status": status,
                "start": list(item.start_px), "end": list(item.end_px),
                "widthMm": item.width_mm, "reasonCodes": list(item.reason_codes),
            })
    elif vision_stage is not None and vision_stage.status == "accepted":
        draft = vision_stage.data.get("draft")
        for index, item in enumerate(getattr(draft, "openings", ()), 1):
            start, end = _point(item.pixelStart, width, height), _point(item.pixelEnd, width, height)
            _draw_line(fusion_overlay, start, end, COLORS["raw"], f"P{index}")
            predictions.append({
                "id": f"P{index}", "type": item.type.value, "status": "raw",
                "start": list(start), "end": list(end),
                "widthMm": None, "reasonCodes": ["FUSION_UNAVAILABLE"],
            })

    original_name = f"{image_path.stem}.original.png"
    gaps_name = f"{image_path.stem}.cv-gaps.jpg"
    arcs_name = f"{image_path.stem}.cv-arcs.jpg"
    fusion_name = f"{image_path.stem}.fusion-openings.jpg"
    shutil.copy2(image_path, output_dir / original_name)
    for filename, layer in ((gaps_name, gap_overlay), (arcs_name, arc_overlay), (fusion_name, fusion_overlay)):
        cv2.imwrite(str(output_dir / filename), layer, [cv2.IMWRITE_JPEG_QUALITY, 94])
    return {
        "sampleId": image_path.stem,
        "original": original_name,
        "layers": {"gaps": gaps_name, "arcs": arcs_name, "fusion": fusion_name},
        "sourceSha256": hashlib.sha256(payload).hexdigest(),
        "parseStatus": existing["parseStatus"] if existing is not None else run.status.value,
        "reasonCodes": existing["reasonCodes"] if existing is not None else list(run.reasonCodes),
        "cvGapCount": len(gaps),
        "predictions": predictions,
        "stageSummary": existing["stageSummary"] if existing is not None else [
            {"stage": stage.stage, "status": stage.status, "detail": stage.detail}
            for stage in run.stages
        ],
    }


def _html(report: dict) -> str:
    data = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
<title>门窗自动识别评价</title><style>
body{{margin:0;background:#111827;color:#e5e7eb;font:14px/1.45 system-ui,'Microsoft YaHei';}}main{{max-width:1800px;margin:auto;padding:24px}}h1{{margin:0 0 8px}}.muted{{color:#9ca3af}}.legend{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}}.dot{{width:11px;height:11px;border-radius:50%;display:inline-block;margin-right:5px}}.card{{background:#1f2937;border:1px solid #374151;border-radius:14px;padding:16px;margin:18px 0}}.images{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}img{{width:100%;background:white;border-radius:8px}}table{{width:100%;border-collapse:collapse;margin-top:12px}}td,th{{border-bottom:1px solid #374151;padding:7px;text-align:left}}select,input{{background:#111827;color:#e5e7eb;border:1px solid #4b5563;border-radius:6px;padding:6px}}button{{position:sticky;bottom:18px;background:#2563eb;color:white;border:0;border-radius:9px;padding:12px 18px;font-weight:700;cursor:pointer}}@media(max-width:1000px){{.images{{grid-template-columns:1fr 1fr}}}}@media(max-width:650px){{.images{{grid-template-columns:1fr}}}}
</style></head><body><main><h1>门窗自动识别人工评价</h1><p class='muted'>左侧原图，右侧算法叠加。P编号用于逐条评价；G是CV物理缺口，只作证据，不等于已确认门窗。评价保存在当前浏览器，可导出JSON。</p>
<div class='legend'><span><i class='dot' style='background:#28be28'></i>confirmed</span><span><i class='dot' style='background:#ffa500'></i>candidate</span><span><i class='dot' style='background:#dc2828'></i>rejected</span><span><i class='dot' style='background:#dc3cdc'></i>视觉原始提议</span><span><i class='dot' style='background:#00b4ff'></i>A：CV门弧</span><span><i class='dot' style='background:#ffdc00'></i>G：CV物理缺口</span></div><div id='cards'></div><button id='export'>导出评价 JSON</button>
<script>const REPORT={data};const KEY='door-window-review-v2-'+REPORT.reportHash;let feedback=JSON.parse(localStorage.getItem(KEY)||'{{}}');
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[c]));
function save(){{localStorage.setItem(KEY,JSON.stringify(feedback));}}
function setv(sample,id,value){{feedback[sample]??={{predictions:{{}},overall:'',notes:''}};if(id==='overall'||id==='notes')feedback[sample][id]=value;else feedback[sample].predictions[id]=value;save();}}
document.getElementById('cards').innerHTML=REPORT.samples.map(s=>{{const f=feedback[s.sampleId]||{{predictions:{{}}}};return `<section class='card'><h2>${{esc(s.sampleId)}} · 可评价提议${{s.predictions.length}} · CV缺口${{s.cvGapCount}} · ${{esc(s.parseStatus)}}</h2><div class='images'><div><div>原图</div><img src='${{esc(s.original)}}'></div><div><div>G：CV物理缺口</div><img src='${{esc(s.layers.gaps)}}'></div><div><div>A：CV门弧候选</div><img src='${{esc(s.layers.arcs)}}'></div><div><div>P：视觉/融合结果</div><img src='${{esc(s.layers.fusion)}}'></div></div><table><thead><tr><th>编号</th><th>类型</th><th>状态</th><th>原因</th><th>你的评价</th></tr></thead><tbody>${{s.predictions.map(p=>`<tr><td>${{p.id}}</td><td>${{p.type}}</td><td>${{p.status}}</td><td>${{esc(p.reasonCodes.join(','))}}</td><td><select onchange="setv('${{s.sampleId}}','${{p.id}}',this.value)"><option value=''>未评</option>${{['正确门','正确窗','正确垭口','误检','位置错'].map(v=>`<option ${{f.predictions?.[p.id]===v?'selected':''}}>${{v}}</option>`).join('')}}</select></td></tr>`).join('')}}</tbody></table><p>整图评价：<select onchange="setv('${{s.sampleId}}','overall',this.value)"><option value=''>未评</option>${{['基本正确','有漏检','有误检','有漏检也有误检'].map(v=>`<option ${{f.overall===v?'selected':''}}>${{v}}</option>`).join('')}}</select>　备注：<input value='${{esc(f.notes||'')}}' onchange="setv('${{s.sampleId}}','notes',this.value)" placeholder='例如：卫生间门漏了'></p></section>`;}}).join('');
document.getElementById('export').onclick=()=>{{const blob=new Blob([JSON.stringify({{schemaVersion:'0.1.0',reportHash:REPORT.reportHash,feedback}},null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='door-window-feedback.json';a.click();URL.revokeObjectURL(a.href);}};</script></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assisted", action="store_true")
    parser.add_argument("--allow-external-vision", action="store_true")
    parser.add_argument("--reuse-report", type=Path)
    args = parser.parse_args()
    if args.assisted and not args.allow_external_vision and args.reuse_report is None:
        raise SystemExit("--assisted requires --allow-external-vision")
    config = None
    mode = VisionMode.OFF
    if args.assisted and args.reuse_report is None:
        from pano_platform.vision.config import VisionConfig

        config = VisionConfig.from_env()
        mode = VisionMode.ASSISTED
    args.output_dir.mkdir(parents=True, exist_ok=True)
    existing = None
    if args.reuse_report is not None:
        previous = json.loads(args.reuse_report.read_text(encoding="utf-8"))
        existing = {item["sampleId"]: item for item in previous["samples"]}
        mode = VisionMode(previous.get("mode", "off"))
    samples = []
    for image_path in sorted(args.image_dir.glob("*.png")):
        print(f"[{len(samples) + 1}] {image_path.name}", flush=True)
        samples.append(render_sample(image_path, args.output_dir, mode, config, existing.get(image_path.stem) if existing else None))
    report = {"schemaVersion": "0.1.0", "mode": mode.value, "samples": samples}
    report["reportHash"] = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "index.html").write_text(_html(report), encoding="utf-8")
    print(f"review -> {args.output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
