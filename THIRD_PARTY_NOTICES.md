# Third-Party Notices

## floorplan-3d

Portions of `apps/pano-viewer/src/showcase.ts` and `apps/pano-viewer/src/Floorplan3D.tsx`
(interactive showcase: lighting / view presets / first-person walk / joystick / door interaction)
are adapted from https://github.com/wy51ai/floorplan-3d .

MIT License

Copyright (c) 2026 wuyi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## CubiCasa5K（可选，默认不启用）

- 来源：https://github.com/CubiCasa/CubiCasa5k
- 许可：CC BY-NC 4.0（Copyright (c) 2019 CubiCasa），仅限非商业使用；本项目为个人非商业项目。
- 用法：`scripts/fetch_cubicasa.sh` 在运行时下载代码与预训练权重到 `tmp/models/cubicasa5k`（已被 Git 忽略），仓库不包含其代码或权重。未下载或未安装 torch 时自动回退到纯 CV 路径。
