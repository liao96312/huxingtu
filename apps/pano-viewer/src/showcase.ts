/*
 * Interactive showcase helpers for the Floorplan3D viewer (interactive rgb mode only).
 *
 * Portions adapted from floorplan-3d (https://github.com/wy51ai/floorplan-3d), index.html:
 * walk controls / stepWalk, camTween view presets, sun lighting setup, walk-only edge lines
 * and baseboards, door open/close interaction.
 * Copyright (c) 2026 wuyi — MIT License. See THIRD_PARTY_NOTICES.md at the repository root.
 * Collision was rewritten for arbitrary-angle walls (point-to-segment distance).
 */
import * as THREE from "three";

export type Point2 = { x: number; y: number };
export type Segment = { ax: number; az: number; bx: number; bz: number };

/** Business (mm, y-north) → world (m, x / -z). */
export const toWorld = (p: Point2) => ({ x: p.x / 1000, z: -p.y / 1000 });

export function polygonSegments(points: Point2[]): Segment[] {
  const segs: Segment[] = [];
  for (let i = 0; i < points.length; i++) {
    const a = toWorld(points[i]), b = toWorld(points[(i + 1) % points.length]);
    if (Math.hypot(b.x - a.x, b.z - a.z) > 1e-6) segs.push({ ax: a.x, az: a.z, bx: b.x, bz: b.z });
  }
  return segs;
}

export function segmentDistance(px: number, pz: number, s: Segment): number {
  const dx = s.bx - s.ax, dz = s.bz - s.az;
  const len2 = dx * dx + dz * dz;
  const t = len2 ? Math.max(0, Math.min(1, ((px - s.ax) * dx + (pz - s.az) * dz) / len2)) : 0;
  return Math.hypot(px - (s.ax + t * dx), pz - (s.az + t * dz));
}

export const blocked = (x: number, z: number, segs: Segment[], radius: number) =>
  segs.some((s) => segmentDistance(x, z, s) < radius);

export function polygonArea(points: Point2[]): number {
  let a = 0;
  for (let i = 0; i < points.length; i++) {
    const p = points[i], q = points[(i + 1) % points.length];
    a += p.x * q.y - q.x * p.y;
  }
  return Math.abs(a) / 2;
}

export function polygonCentroid(points: Point2[]): Point2 {
  let cx = 0, cy = 0, a = 0;
  for (let i = 0; i < points.length; i++) {
    const p = points[i], q = points[(i + 1) % points.length];
    const f = p.x * q.y - q.x * p.y;
    a += f; cx += (p.x + q.x) * f; cy += (p.y + q.y) * f;
  }
  if (Math.abs(a) < 1e-9) {
    return { x: points.reduce((s, p) => s + p.x, 0) / points.length, y: points.reduce((s, p) => s + p.y, 0) / points.length };
  }
  return { x: cx / (3 * a), y: cy / (3 * a) };
}

const ROOM_NAMES: [RegExp, string][] = [
  [/living/, "客厅"], [/dining/, "餐厅"], [/kitchen/, "厨房"], [/master/, "主卧"], [/bed/, "卧室"],
  [/bath|toilet|wc/, "卫生间"], [/balcony/, "阳台"], [/study/, "书房"], [/hall|corridor|aisle/, "过道"],
  [/laundry/, "洗衣房"], [/entry|foyer/, "玄关"], [/storage/, "储藏间"],
];
/** Human label from a roomId like "room_bed_left"; returns null when nothing sensible is known. */
export function roomLabel(roomId: string | undefined): string | null {
  if (!roomId) return null;
  const key = roomId.toLowerCase();
  for (const [re, name] of ROOM_NAMES) if (re.test(key)) return name;
  return roomId.replace(/^room_/, "");
}

/** Sun direction for an hour of day (6–19), simplified arc as in floorplan-3d's sun slider. */
export function sunForHour(hour: number, center: THREE.Vector3, radius: number) {
  const t = Math.max(0, Math.min(1, (hour - 6) / 13));
  const az = Math.PI * (0.15 + 0.7 * t);
  const el = Math.sin(Math.PI * t) * 1.05 + 0.12;
  const pos = new THREE.Vector3(
    center.x + Math.cos(az) * Math.cos(el) * radius,
    center.y + Math.sin(el) * radius,
    center.z + Math.sin(az) * Math.cos(el) * radius * 0.8,
  );
  const warm = 1 - Math.sin(Math.PI * t);
  const color = new THREE.Color(0xfff1dd).lerp(new THREE.Color(0xffb36b), warm * 0.8);
  const intensity = 0.8 + 2.0 * Math.sin(Math.PI * t);
  return { pos, color, intensity };
}

export type Pose = { pos: THREE.Vector3; target: THREE.Vector3 };
export type Tween = { from: Pose; to: Pose; t0: number; dur: number };

export function tweenPose(tw: Tween, now: number): { pose: Pose; done: boolean } {
  const k = Math.min(1, (now - tw.t0) / tw.dur);
  const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
  return {
    pose: { pos: tw.from.pos.clone().lerp(tw.to.pos, e), target: tw.from.target.clone().lerp(tw.to.target, e) },
    done: k >= 1,
  };
}

export type ViewPreset = "bird" | "iso" | "top";
export function presetPose(preset: ViewPreset, box: THREE.Box3): Pose {
  const c = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const span = Math.max(size.x, size.z, 4);
  const target = new THREE.Vector3(c.x, 0.8, c.z);
  if (preset === "top") return { pos: new THREE.Vector3(c.x, span * 1.45, c.z + 0.01), target };
  if (preset === "iso") return { pos: new THREE.Vector3(c.x + span * 0.75, span * 0.75, c.z + span * 0.75), target };
  return { pos: new THREE.Vector3(c.x + span * 0.35, span * 0.95, c.z + span * 0.85), target };
}
