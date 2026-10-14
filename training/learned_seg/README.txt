户型图识别模型 · 一键训练包（GTX 1660 Super 等 NVIDIA 显卡）

1. 把整个压缩包解压到一个空间够的盘，比如 D:\huxingtu-train（建议路径不要有中文和空格，至少留 6GB 空间）。
2. 双击 start_training.bat。第一次会自动安装 Python 和显卡版 PyTorch（约 3GB 下载，可能要 10~40 分钟）。
3. 看到“冒烟测试通过”后会自动开始正式训练，窗口里会显示进度和剩余时间。放着过夜即可，期间电脑不要关机（程序会阻止电脑自动睡眠）。
4. 中途关掉窗口或断电也没关系，再次双击 start_training.bat 会从上一轮接着训练。
5. 出现“全部完成”后，会自动打开 output 文件夹。把里面的 best.pth（约 50MB）发回给我。
   装了 GitHub 命令行 gh 的话，也可以双击 send_back.bat 自动上传。

遇到报错：把窗口截图发给我即可。
数据来源：ResPlan（CC BY 4.0）；模型：segmentation_models_pytorch U-Net + ResNet34（MIT）。
