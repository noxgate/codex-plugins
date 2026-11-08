# 组合图拆分工作流

当提示词明确要求多个素材或多张图片时，主动评估同图生产，不要求用户另行指定组合图。适合的同类游戏素材、图标或其他可清晰分区的内容可生成源图 sheet，再按交付要求拆成多个独立资产；独立场景、高细节大图、连续动画帧或无法可靠拆分的内容分别生成。用户明确要求逐张独立生成时遵从该要求。源图张数与最终交付数量分开记录，根据画布与物体复杂度分组，通常 4～8 件，也可更少，不为凑数添加素材或把整个素材库塞在一张图里。

## 配置

生成manifest沿用 `asset_batch.py`，`id` 是sheet任务ID，`n=1` 表示一张组合源图；指定纯色背景时用 `background=opaque`。源图不要使用单个成员的 `target_size`，避免提前缩小或裁切整张sheet。约定不可见规则分区，每格一件且轮廓互不接触，禁止标签、地面与共同阴影，预留15%左右留白。

拆分manifest为 `version=1.0`、`sheets` 数组，每张sheet包含：

- `id/source/size`：任务ID、相对manifest的源文件路径、准确源图尺寸。
- `key_color/background_tolerance/foreground_threshold`：建议对绿植用 `#FF00FF/35/160`。背景色不得与物体颜色相近。
- `margin_px`：输出画布四边最小留白。
- `items`：每项 `asset_id`、归一化 `[x0,y0,x1,y1]` 的 `rect`、`target_size`、`anchor`；`max_components` 默认1，竹丛等显式允许多个组成物体。
- `empty_rects`：预留空白格，检测额外生成的物体。

画布实际尺寸不符、分区重叠、空格出现内容、轮廓裁切或物体数异常时停止，不静默猜测新布局。当前裁切将物体等比居中放入目标画布，anchor记录其展示锚点；若需要脚底对齐而非中心居中，应先明确新的摆放策略。

## 命令

```bash
python3 scripts/sheet_batch.py plan --manifest /absolute/path/split-manifest.json
python3 scripts/sheet_batch.py split --manifest /absolute/path/split-manifest.json --output-dir /absolute/path/extracted
python3 scripts/sheet_batch.py validate --manifest /absolute/path/split-manifest.json --output-dir /absolute/path/extracted
```

脚本需要Pillow与NumPy。边缘实际色键由每格边框测量；去色键后处理抗锯齿混色并记录无法估计的边缘。低透明度的孤立噪声可清理；大面积半透明物体须原生透明图或专用抠图，脚本会拒绝超出窄轮廓的混色区域，不冒充通用AI分割器。

产物为独立RGBA PNG、`review.png` 和 `split-report.json`。报告记录源图SHA256和成员ID，状态固定待美术审核；工具不能判断一株树是否正确画成松树。通过机器校验后逐件查看浅/深/棋盘背景并在目标展示尺寸下审核，再验收整组统一性。缺件或一件形态错误可参考原sheet做定点修正版，不自动无限重生成。分组样板未批准时只生成当前样板范围。
