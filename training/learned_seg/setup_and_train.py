"""One-click setup + training for Windows (called by 开始训练.bat).

Steps: create venv -> detect NVIDIA driver -> install CUDA torch -> install
deps (Tsinghua mirror) -> check GPU -> ~1 minute smoke test -> full training.
Re-running is safe: finished steps are skipped and training resumes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / "venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"
TORCH = ("torch==2.5.1", "torchvision==0.20.1")


def say(message: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


def run(cmd: list[str], check: bool = True) -> int:
    say("执行: " + " ".join(str(c) for c in cmd))
    code = subprocess.call([str(c) for c in cmd])
    if check and code != 0:
        raise SystemExit(f"\n出错了（返回码 {code}）。请把这个窗口截图发给我。")
    return code


def driver_version() -> tuple[str, float] | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            text=True, timeout=30,
        ).strip().splitlines()[0]
    except Exception:
        return None
    name, version = [part.strip() for part in out.split(",")[:2]]
    match = re.match(r"(\d+)\.(\d+)", version)
    return name, float(f"{match.group(1)}.{match.group(2)}") if match else 0.0


def torch_ok() -> bool:
    code = subprocess.call([str(PY), "-c", "import torch,sys;sys.exit(0 if torch.cuda.is_available() and torch.__version__.startswith('2.5.1') else 1)"])
    return code == 0


def main() -> int:
    say("==== 第 1 步：检查显卡驱动 ====")
    info = driver_version()
    if info is None:
        say("没有找到 nvidia-smi。请先到 https://www.nvidia.cn/drivers/ 安装最新 GeForce 驱动，重启后再双击。")
        return 1
    name, version = info
    say(f"显卡: {name}，驱动版本: {version}")
    if version >= 531:
        cuda = "cu121"
    elif version >= 522:
        cuda = "cu118"
    else:
        say("驱动太旧（需要 522 以上）。请到 https://www.nvidia.cn/drivers/ 更新驱动，重启后再双击。")
        return 1

    say("==== 第 2 步：准备 Python 虚拟环境 ====")
    if not PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([PY, "-m", "pip", "install", "-q", "--upgrade", "pip", "-i", TUNA], check=False)

    say(f"==== 第 3 步：安装显卡版 PyTorch（{cuda}，约 2.5GB，第一次比较慢）====")
    if torch_ok():
        say("PyTorch 已安装，跳过。")
    else:
        code = run([PY, "-m", "pip", "install", *TORCH, "--index-url", f"https://download.pytorch.org/whl/{cuda}"], check=False)
        if code != 0 or not torch_ok():
            say("官方源失败，改用阿里云镜像重试……")
            run([PY, "-m", "pip", "install", *(f"{t}+{cuda}" for t in TORCH),
                 "-f", f"https://mirrors.aliyun.com/pytorch-wheels/{cuda}/"])

    say("==== 第 4 步：安装其他依赖（清华镜像）====")
    run([PY, "-m", "pip", "install", "-q", "-r", HERE / "requirements.txt", "-c", HERE / "constraints.txt", "-i", TUNA])

    say("==== 第 5 步：检查显卡能否被 PyTorch 使用 ====")
    if not torch_ok():
        say("PyTorch 看不到显卡。请把这个窗口截图发给我。")
        return 1
    run([PY, "-c", "import torch;print('CUDA 可用:', torch.cuda.get_device_name(0))"])

    amp_file = HERE / "output" / "amp_mode.txt"
    amp = amp_file.read_text().strip() if amp_file.exists() else ""
    if not (HERE / "output" / "last.pth").exists():
        say("==== 第 6 步：1 分钟冒烟测试 ====")
        amp = "auto"
        if run([PY, HERE / "train_seg.py", "--smoke", "--amp", "auto"], check=False) != 0:
            say("半精度测试失败，改用全精度再试一次……")
            amp = "off"
            run([PY, HERE / "train_seg.py", "--smoke", "--amp", "off"])
        (HERE / "output").mkdir(exist_ok=True)
        amp_file.write_text(amp)

    say("==== 第 7 步：正式训练（预计一个晚上，可以随时关掉，再次双击会接着训练）====")
    run([PY, HERE / "train_seg.py", "--amp", amp or "auto"])
    say(f"全部完成！请把这个文件发给我：{HERE / 'output' / 'best.pth'}")
    if os.name == "nt":
        subprocess.call(["explorer", str(HERE / "output")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
