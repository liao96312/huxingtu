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
        cls = torch.from_numpy((label & 7).astype(np.int64).copy())
        boundary = torch.from_numpy(((label >> 3) & 1).astype(np.float32).copy())
        return to_tensor(np.ascontiguousarray(image)), cls, boundary


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
    bce = F.binary_cross_entropy_with_logits(
        logits[:, NUM_CLASSES].float(), boundary, pos_weight=torch.tensor(5.0, device=logits.device)
    )
    return ce + bce


@torch.no_grad()
def evaluate(model, loader, device, use_amp: bool, max_batches: int = 0) -> dict:
    model.eval()
    inter = torch.zeros(NUM_CLASSES + 1, dtype=torch.float64)
    union = torch.zeros(NUM_CLASSES + 1, dtype=torch.float64)
    for index, (image, cls, boundary) in enumerate(loader):
        if max_batches and index >= max_batches:
            break
        image, cls, boundary = image.to(device), cls.to(device), boundary.to(device)
        with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
            logits = model(image)
        pred = logits[:, :NUM_CLASSES].argmax(1)
        for c in range(NUM_CLASSES):
            p, t = pred == c, cls == c
            inter[c] += (p & t).sum().item()
            union[c] += (p | t).sum().item()
        pb, tb = logits[:, NUM_CLASSES] > 0, boundary > 0.5
        inter[NUM_CLASSES] += (pb & tb).sum().item()
        union[NUM_CLASSES] += (pb | tb).sum().item()
    model.train()
    iou = (inter / union.clamp(min=1)).tolist()
    score = float(np.mean(iou[1:]))  # wall, door, window, room, boundary
    return {"iou": [round(v, 4) for v in iou], "score": round(score, 4)}


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
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch", type=int, default=6)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--amp", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--max-hours", type=float, default=11.0)
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

    model = build_model(args.encoder_weights).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    use_amp = device.type == "cuda" and args.amp != "off"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    epochs = 1 if args.smoke else args.epochs
    steps_per_epoch = len(train_loader)
    total_steps = epochs * steps_per_epoch
    start_epoch, best, bad_epochs, history = 0, -1.0, 0, []

    last_path, best_path = out / "last.pth", out / "best.pth"
    if last_path.exists() and not args.smoke:
        state = torch.load(str(last_path), map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start_epoch, best, bad_epochs, history = state["epoch"] + 1, state["best"], state["bad_epochs"], state["history"]
        use_amp = use_amp and state.get("use_amp", True)
        scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
        log(f"检测到上次的进度，从第 {start_epoch + 1} 轮继续（目前最好得分 {best:.4f}）")
    if start_epoch >= epochs:
        log("已经训练完所有轮次。best.pth 就在 output 文件夹里。")
        return 0

    log(f"训练集 {len(train_set)} 张，验证集 {len(val_set)} 张，每轮 {steps_per_epoch} 步，共 {epochs} 轮，"
        f"混合精度(fp16): {'开' if use_amp else '关'}")
    started = time.time()
    bad_steps = 0
    for epoch in range(start_epoch, epochs):
        model.train()
        epoch_start = time.time()
        running = 0.0
        for step, (image, cls, boundary) in enumerate(train_loader):
            global_step = epoch * steps_per_epoch + step
            for group in optimizer.param_groups:
                group["lr"] = lr_at(global_step, total_steps, args.lr, warmup=min(300, total_steps // 10 + 1))
            image = image.to(device, non_blocking=True)
            cls, boundary = cls.to(device, non_blocking=True), boundary.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
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
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            running += loss.item()
            if (step + 1) % 50 == 0 or step + 1 == steps_per_epoch or args.smoke:
                done = global_step + 1 - start_epoch * steps_per_epoch
                rate = done / max(1e-6, time.time() - started)
                left_epoch = (steps_per_epoch - step - 1) / max(rate, 1e-6)
                left_total = (total_steps - global_step - 1) / max(rate, 1e-6)
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
                        "input_size": 512, "epoch": epoch + 1, "metrics": metrics}, str(best_path))
        else:
            bad_epochs += 1
        torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(), "epoch": epoch,
                    "best": best, "bad_epochs": bad_epochs, "history": history, "use_amp": use_amp},
                   str(last_path))
        (out / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"第 {epoch + 1} 轮完成：验证 IoU {named}  得分 {metrics['score']:.4f}"
            + ("  ★ 新最好，已保存 best.pth" if improved else f"  （连续 {bad_epochs} 轮没有提升）"))
        if args.smoke:
            log("冒烟测试通过：数据、模型、显卡、保存都正常。")
            return 0
        if bad_epochs >= args.patience:
            log(f"连续 {args.patience} 轮没有提升，提前结束。")
            break
        if time.time() - started > args.max_hours * 3600:
            log(f"已达到 {args.max_hours} 小时上限，先停在这里。再次双击可继续。")
            break
    log(f"训练结束。最好得分 {best:.4f}。要发回的文件：{best_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
