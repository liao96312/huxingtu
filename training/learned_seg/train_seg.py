"""Train the huxingtu floor-plan segmentation model (U-Net / ResNet34).

Outputs 6 channels: 5 classes (background, wall, door, window, room) and one
room-boundary logit. Resumable: re-running continues from output/last.pth.
Messages are printed in Simplified Chinese for the end user.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

NUM_CLASSES = 5
CLASS_NAMES_ZH = ("背景", "墙", "门", "窗", "房间")
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def log(message: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


def read_png(path: Path) -> np.ndarray:
    # np.fromfile + imdecode works with non-ASCII Windows paths; cv2.imread does not.
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"无法读取图片: {path}")
    return image


def to_tensor(gray: np.ndarray) -> torch.Tensor:
    rgb = np.repeat((gray.astype(np.float32) / 255.0)[..., None], 3, axis=2)
    return torch.from_numpy(((rgb - MEAN) / STD).transpose(2, 0, 1).copy())


class PlanDataset(Dataset):
    def __init__(self, root: Path, augment: bool, limit: int = 0) -> None:
        self.with_instances = (root / "inst").is_dir() and not augment
        self.images = sorted((root / "img").glob("*.png"))
        if limit:
            self.images = self.images[:limit]
        self.root = root
        self.augment = augment

    def __len__(self) -> int:
        return len(self.images)

    def __getitem__(self, index: int):
        path = self.images[index]
        image = read_png(path)
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        label = read_png(self.root / "lbl" / path.name)
        if self.augment:
            k = random.randint(0, 3)
            image, label = np.rot90(image, k), np.rot90(label, k)
            if random.random() < 0.5:
                image, label = image[:, ::-1], label[:, ::-1]
            image, label = random_scale(np.ascontiguousarray(image), np.ascontiguousarray(label))
            image = jitter(image)
        cls = torch.from_numpy((label & 7).astype(np.int64).copy())
        boundary = torch.from_numpy(((label >> 3) & 1).astype(np.float32).copy())
        if self.with_instances:
            inst = torch.from_numpy(read_png(self.root / "inst" / path.name).astype(np.int64))
        else:
            inst = torch.zeros(1, dtype=torch.int64)
        return to_tensor(np.ascontiguousarray(image)), cls, boundary, inst


def random_scale(image: np.ndarray, label: np.ndarray, low: float = 0.8, high: float = 1.2):
    """Zoom in/out around the centre; pad with white image / background label."""

    size = image.shape[0]
    factor = random.uniform(low, high)
    new = max(8, int(round(size * factor)))
    image = cv2.resize(image, (new, new), interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_LINEAR)
    label = cv2.resize(label, (new, new), interpolation=cv2.INTER_NEAREST)
    if new >= size:
        offset = (new - size) // 2
        return image[offset:offset + size, offset:offset + size], label[offset:offset + size, offset:offset + size]
    pad = size - new
    top, left = pad // 2, pad // 2
    out_image = np.full((size, size), 255, np.uint8)
    out_label = np.zeros((size, size), np.uint8)
    out_image[top:top + new, left:left + new] = image
    out_label[top:top + new, left:left + new] = label
    return out_image, out_label


def jitter(image: np.ndarray) -> np.ndarray:
    """Brightness/contrast jitter so grey-filled real plans look less foreign."""

    alpha = random.uniform(0.75, 1.15)
    beta = random.uniform(-30, 30)
    return np.clip(image.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)


def build_model(encoder_weights: Path | None):
    import segmentation_models_pytorch as smp

    model = smp.Unet(encoder_name="resnet34", encoder_weights=None, classes=NUM_CLASSES + 1)
    if encoder_weights and encoder_weights.exists():
        state = torch.load(str(encoder_weights), map_location="cpu")
        own = model.encoder.state_dict()
        matched = {k: v for k, v in state.items() if k in own and own[k].shape == v.shape}
        own.update(matched)
        model.encoder.load_state_dict(own)
        log(f"已加载 ImageNet 预训练编码器（{len(matched)}/{len(own)} 个参数）")
    else:
        log("未找到预训练编码器权重，从头训练（效果会差一些）")
    return model


CLASS_WEIGHTS = torch.tensor([1.0, 2.0, 4.0, 4.0, 1.0])


def compute_loss(logits, cls, boundary):
    ce = F.cross_entropy(logits[:, :NUM_CLASSES].float(), cls, weight=CLASS_WEIGHTS.to(logits.device))
    edge = logits[:, NUM_CLASSES].float()
    bce = F.binary_cross_entropy_with_logits(edge, boundary, pos_weight=torch.tensor(5.0, device=logits.device))
    prob = torch.sigmoid(edge)
    dice = 1 - (2 * (prob * boundary).sum() + 1) / (prob.sum() + boundary.sum() + 1)
    return ce + BOUNDARY_WEIGHT * (bce + dice)


BOUNDARY_WEIGHT = 2.0


@torch.no_grad()
def evaluate(model, loader, device, use_amp: bool, max_batches: int = 0) -> dict:
    model.eval()
    inter = torch.zeros(NUM_CLASSES + 1, dtype=torch.float64)
    union = torch.zeros(NUM_CLASSES + 1, dtype=torch.float64)
    exact, room_iou, images = 0, 0.0, 0
    for index, (image, cls, boundary, inst) in enumerate(loader):
        if max_batches and index >= max_batches:
            break
        image, cls, boundary = image.to(device), cls.to(device), boundary.to(device)
        with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
            logits = model(image)
        pred = logits[:, :NUM_CLASSES].argmax(1)
        if inst.dim() == 3:
            free = ((pred == 4) & (logits[:, NUM_CLASSES] <= 0)).cpu().numpy()
            for b in range(free.shape[0]):
                count, iou = room_instance_score(free[b], inst[b].numpy())
                exact += count
                room_iou += iou
                images += 1
        for c in range(NUM_CLASSES):
            p, t = pred == c, cls == c
            inter[c] += (p & t).sum().item()
            union[c] += (p | t).sum().item()
        pb, tb = logits[:, NUM_CLASSES] > 0, boundary > 0.5
        inter[NUM_CLASSES] += (pb & tb).sum().item()
        union[NUM_CLASSES] += (pb | tb).sum().item()
    model.train()
    iou = (inter / union.clamp(min=1)).tolist()
    pixel = float(np.mean(iou[1:]))  # wall, door, window, room, boundary
    if images:
        exact_rate, mean_room_iou = exact / images, float(room_iou / images)
        score = 0.5 * exact_rate + 0.5 * mean_room_iou
    else:  # datasets without instances (v1 layout): fall back to pixel IoU
        exact_rate = mean_room_iou = float("nan")
        score = pixel
    return {"iou": [round(v, 4) for v in iou], "pixel": round(pixel, 4),
            "roomCountExact": round(exact_rate, 4), "roomIoU": round(mean_room_iou, 4), "score": round(score, 4)}


MIN_ROOM_FRACTION = 0.001  # ~400 px at 640^2: drops specks, keeps toilets


def room_instance_score(free: np.ndarray, truth: np.ndarray) -> tuple[int, float]:
    """(1 if room count matches, greedy one-to-one mean IoU / max(count))."""

    count, labels, stats, _ = cv2.connectedComponentsWithStats(free.astype(np.uint8), connectivity=4)
    minimum = MIN_ROOM_FRACTION * free.size
    predicted = [labels == i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= minimum]
    truths = [truth == v for v in np.unique(truth) if v != 0]
    if not predicted and not truths:
        return 1, 1.0
    if not predicted or not truths:
        return 0, 0.0
    pairs = []
    for i, p in enumerate(predicted):
        for j, t in enumerate(truths):
            inter = np.logical_and(p, t).sum()
            if inter:
                pairs.append((inter / np.logical_or(p, t).sum(), i, j))
    used_p, used_t, total = set(), set(), 0.0
    for value, i, j in sorted(pairs, reverse=True):
        if i not in used_p and j not in used_t:
            used_p.add(i)
            used_t.add(j)
            total += value
    return int(len(predicted) == len(truths)), total / max(len(predicted), len(truths))


def lr_at(step: int, total: int, base: float, warmup: int = 300) -> float:
    if step < warmup:
        return base * (step + 1) / warmup
    progress = min(1.0, (step - warmup) / max(1, total - warmup))
    return base * (0.02 + 0.98 * 0.5 * (1 + math.cos(math.pi * progress)))


def fmt_seconds(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}小时{seconds % 3600 // 60:02d}分"


def keep_awake() -> None:
    """Stop Windows from sleeping during an overnight run (screen may still turn off)."""

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass


def main() -> int:
    keep_awake()
    parser = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    parser.add_argument("--data", type=Path, default=here / "data")
    parser.add_argument("--out", type=Path, default=here / "output")
    parser.add_argument("--encoder-weights", type=Path, default=here / "weights" / "resnet34_imagenet_encoder.pth")
    parser.add_argument("--init", type=Path, default=here / "weights" / "v1_best.pth",
                        help="v1 模型权重，用来热启动（全卷积网络，分辨率变化不影响）")
    parser.add_argument("--epochs", type=int, default=6)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--accum", type=int, default=2, help="梯度累积步数（等效 batch = batch × accum）")
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--amp", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--max-hours", type=float, default=12.0, help="累计训练时长上限（含断点续训）")
    parser.add_argument("--limit-train", type=int, default=0)
    parser.add_argument("--smoke", action="store_true", help="约1分钟的冒烟测试，不覆盖正式结果")
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() and not args.cpu else "cpu")
    if device.type == "cuda":
        log(f"使用显卡: {torch.cuda.get_device_name(0)}，显存 {torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} GB")
        torch.backends.cudnn.benchmark = True
    else:
        log("警告：没有检测到可用显卡，正在用 CPU（会非常慢，只适合测试）")

    out = args.out / "smoke" if args.smoke else args.out
    out.mkdir(parents=True, exist_ok=True)
    train_set = PlanDataset(args.data / "train", augment=True, limit=args.limit_train or (60 if args.smoke else 0))
    val_set = PlanDataset(args.data / "val", augment=False, limit=12 if args.smoke else 0)
    if not len(train_set) or not len(val_set):
        log(f"错误：在 {args.data} 下找不到训练数据（需要 train/img、train/lbl、val/img、val/lbl）")
        return 2
    loader_kwargs = dict(num_workers=args.workers, pin_memory=device.type == "cuda",
                         persistent_workers=args.workers > 0)
    train_loader = DataLoader(train_set, batch_size=args.batch, shuffle=True, drop_last=True, **loader_kwargs)
    val_loader = DataLoader(val_set, batch_size=args.batch, shuffle=False, **loader_kwargs)

    model = build_model(args.encoder_weights)
    if args.init and args.init.exists() and not (out / "last.pth").exists():
        init = torch.load(str(args.init), map_location="cpu", weights_only=False)
        model.load_state_dict({k: v.float() for k, v in init["model"].items()})
        log(f"已从 v1 模型热启动（v1 第 {init.get('epoch', '?')} 轮，输入 {init.get('input_size', '?')}px → 现在 {train_set.images and read_png(train_set.images[0]).shape[0]}px）")
    model = model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    use_amp = device.type == "cuda" and args.amp != "off"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    epochs = 1 if args.smoke else args.epochs
    steps_per_epoch = len(train_loader)
    total_steps = epochs * steps_per_epoch
    start_epoch, best, bad_epochs, history, elapsed_before = 0, -1.0, 0, [], 0.0
    warmup_steps = min(300, total_steps // 10 + 1)

    last_path, best_path = out / "last.pth", out / "best.pth"
    if last_path.exists() and not args.smoke:
        state = torch.load(str(last_path), map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start_epoch, best, bad_epochs, history = state["epoch"] + 1, state["best"], state["bad_epochs"], state["history"]
        elapsed_before = state.get("elapsed", 0.0)
        use_amp = use_amp and state.get("use_amp", True)
        scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
        log(f"检测到上次的进度，从第 {start_epoch + 1} 轮继续（目前最好得分 {best:.4f}）")
    if elapsed_before >= args.max_hours * 3600 and not args.smoke:
        log(f"已经训练满 {args.max_hours} 小时。best.pth 就在 output 文件夹里。")
        return 0
    if start_epoch >= epochs:
        log("已经训练完所有轮次。best.pth 就在 output 文件夹里。")
        return 0

    log(f"训练集 {len(train_set)} 张，验证集 {len(val_set)} 张，每轮 {steps_per_epoch} 步，共 {epochs} 轮，"
        f"混合精度(fp16): {'开' if use_amp else '关'}")
    started = time.time() - elapsed_before  # hard cap counts earlier sessions too
    session_started = time.time()
    bad_steps = 0
    image_size = read_png(train_set.images[0]).shape[0]
    optimizer.zero_grad(set_to_none=True)
    for epoch in range(start_epoch, epochs):
        model.train()
        epoch_start = time.time()
        running = 0.0
        for step, (image, cls, boundary, _) in enumerate(train_loader):
            global_step = epoch * steps_per_epoch + step
            for group in optimizer.param_groups:  # warmup, then cosine down to 2% over all epochs
                group["lr"] = lr_at(global_step, total_steps, args.lr, warmup=warmup_steps)
            image = image.to(device, non_blocking=True)
            cls, boundary = cls.to(device, non_blocking=True), boundary.to(device, non_blocking=True)
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
                logits = model(image)
            loss = compute_loss(logits, cls, boundary)
            if not torch.isfinite(loss):
                bad_steps += 1
                if use_amp and bad_steps >= 20:
                    use_amp = False
                    scaler = torch.amp.GradScaler("cuda", enabled=False)
                    log("提示：半精度(fp16)连续出现数值异常，已自动切换到全精度(fp32)，会慢一些但更稳")
                    bad_steps = 0
                continue
            bad_steps = 0
            scaler.scale(loss / args.accum).backward()
            if (step + 1) % args.accum == 0 or step + 1 == steps_per_epoch:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
            running += loss.item()
            if (step + 1) % 50 == 0 or step + 1 == steps_per_epoch or args.smoke:
                done = global_step + 1 - start_epoch * steps_per_epoch
                rate = done / max(1e-6, time.time() - session_started)
                left_epoch = (steps_per_epoch - step - 1) / max(rate, 1e-6)
                left_total = min((total_steps - global_step - 1) / max(rate, 1e-6),
                                 args.max_hours * 3600 - (time.time() - started))
                log(f"第 {epoch + 1}/{epochs} 轮  步 {step + 1}/{steps_per_epoch}  损失 {running / (step + 1):.4f}  "
                    f"本轮剩余约 {fmt_seconds(left_epoch)}  全部剩余约 {fmt_seconds(left_total)}")
            if args.smoke and time.time() - epoch_start > 45:
                break
            if time.time() - started > args.max_hours * 3600:
                break
        metrics = evaluate(model, val_loader, device, use_amp)
        named = "  ".join(f"{n}{v:.3f}" for n, v in zip((*CLASS_NAMES_ZH, "边界"), metrics["iou"]))
        improved = metrics["score"] > best
        history.append({"epoch": epoch + 1, "loss": running / max(1, steps_per_epoch), **metrics,
                         "minutes": round((time.time() - epoch_start) / 60, 1)})
        if improved:
            best, bad_epochs = metrics["score"], 0
            torch.save({"model": {k: v.half() for k, v in model.state_dict().items()},
                        "arch": "smp.Unet/resnet34", "classes": NUM_CLASSES, "boundary_channel": NUM_CLASSES,
                        "input_size": image_size, "epoch": epoch + 1, "metrics": metrics,
                        "kit": "v2"}, str(best_path))
        else:
            bad_epochs += 1
        torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(), "epoch": epoch,
                    "best": best, "bad_epochs": bad_epochs, "history": history, "use_amp": use_amp,
                    "elapsed": time.time() - started},
                   str(last_path))
        (out / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"第 {epoch + 1} 轮完成：验证 IoU {named}  房间数全对 {metrics['roomCountExact']:.1%}  房间IoU {metrics['roomIoU']:.3f}  "
            f"得分 {metrics['score']:.4f}  学习率 {optimizer.param_groups[0]['lr']:.1e}"
            + ("  ★ 新最好，已保存 best.pth" if improved else f"  （连续 {bad_epochs} 轮没有提升）"))
        if args.smoke:
            log("冒烟测试通过：数据、模型、显卡、保存都正常。")
            return 0
            break
        if time.time() - started > args.max_hours * 3600:
            log(f"已达到累计 {args.max_hours} 小时上限，训练结束。")
            break
    log(f"训练结束。最好得分 {best:.4f}。要发回的文件：{best_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
