---
name: gpt-image-2-5-game-assets
description: 使用 GPT-Image-2.5 Sunburst/Flare 经 NoxGate 生成游戏美术，支持同类多素材组合图的拆分、色键去背景、独立资产校验与断点续跑，也支持逐帧源图组装为 sprite sheet 和 JSON 帧表；不生成视频。
---

# GPT Image 2.5 游戏素材

在项目中生产素材时，先读取该项目的资产清单、视觉规范与验收要求；不要把其他项目的素材、约定或参考资料带入当前项目。本 skill 本身只包含通用工作流。

## 模型与接口

- `gpt-image-2.5-flare`：快速探索、批量候选；`gpt-image-2.5-sunburst`：定稿、参考图编辑与角色一致性要求高的资产。
- NoxGate 的这两个模型 ID 必须带 `-flare` 或 `-sunburst`。使用当前 Codex 已配置的 provider 和认证，不要求单独填写或保存连接凭据。
- 仅允许 `gpt-image-2.5-flare` 和 `gpt-image-2.5-sunburst`。脚本从 Codex 当前 provider 配置中读取运行所需的连接信息；不得打印、复制到 manifest 或产物元数据。
- 网关的 `POST /images/generations` 可同步返回图片，也可返回任务 ID；异步任务由 `GET /images/generations/{task_id}` 查询。参考图使用上游可访问的 HTTPS URL。不要假设本地文件路径能直接当 `image_urls`。

## 生产流程

1. 从资产清单建立 JSON manifest：每个生成任务有稳定 `id`、用途明确的 `prompt`、模型、源图尺寸、背景和输出数量。任务可以交付单件，也可以是同类多素材sheet；sheet任务ID与拆分成员asset_id分别登记，成员仍独立命名和验收。先生成少量风格样板，选定角色与建筑参考图后再扩量。
2. 使用 `scripts/asset_batch.py plan` 检查清单和模型数量。批量调用时显式设置 `--max-new-tasks`，与用户已授权的生产范围一致。无授权的额外变体、自动重生和无限重试都不要提交。
3. 使用 `scripts/asset_batch.py run` 提交并轮询。脚本把任务 ID 立即写入输出目录的 `_state/`；再次运行会继续轮询或跳过已完成项。下载到本地后校验图片解码、尺寸和透明通道。查看失败项与 `_review/` 中需人工处理的图。
4. 对通过机器校验的素材进行视觉审核：角色面貌一致、武器与阵营色正确、透明边缘干净、图标缩小后可辨认、地块真正无缝。机器校验不代表美术验收。
5. 文字、交互和逻辑元素按当前项目要求由程序实现；帧动画的锚点、循环方式和运动范围以项目资产规范为准。

## 序列帧工作流

当资产清单要求帧动画时，先为每个方向/势力色生成独立帧目录，再运行 `scripts/frame_batch.py`。该脚本不调用模型，负责确定性校验和组装：统一帧尺寸与 RGBA、检查帧数和透明通道、按固定间距排入 power-of-two sprite sheet，并生成包含 `frame`, `x`, `y`, `w`, `h`, `duration_ms`, `anchor` 的 JSON 帧表。锚点、帧数、fps 和循环语义从当前项目的 manifest 读取。

```bash
python3 scripts/frame_batch.py plan --manifest /absolute/path/frames.json
python3 scripts/frame_batch.py assemble --manifest /absolute/path/frames.json --output-dir /absolute/path/atlases
python3 scripts/frame_batch.py validate --manifest /absolute/path/frames.json
```

`assemble` 可重复执行，输出以临时文件替换，适合无人值守断点任务。源帧若尺寸、透明度或数量不合格会失败，不自动裁切或改变锚点；模型生成仍通过 `asset_batch.py`，且只允许 `gpt-image-2.5-flare` 或 `gpt-image-2.5-sunburst`。

## 同类组合图工作流

提示词明确要求多个素材或多张图片时，建立 manifest 前主动评估是否适合组合成一张源图，不必等用户明确说“放在一张图里”。同类游戏素材、成套图标、配色或状态变体，以及其他明显可同图排布的内容，若风格、视角和背景要求兼容，每项都能保留足够细节且可清晰分区，应考虑同图生成；数量较多时按适合的同类小组拆成多张组合图。

独立场景或完整构图、不同尺寸或风格、高细节大图、连续动画帧，以及合图后会混淆、重叠或无法可靠拆分的内容，应分别生成。混合清单只合并适合的部分，其余单独生成；用户明确要求逐张独立生成时遵从该要求。

确定使用组合图后，用一套提示词生成纯色背景的多素材图，统一风格后用 `scripts/sheet_batch.py` 拆成独立 PNG。先读取 [references/sheets.md](references/sheets.md) 中的分区 manifest、去背景适用范围和命令。生成前简要说明分组方式，区分源图张数与最终素材数。最终按用户要求交付整图或拆分后的独立文件，不要仅交付一张组合图冒充多个独立素材，也不要为凑组合图添加用户未要求的成员。固定每格的成员 ID，不依赖模型输出文字标注。框外留白、分区完整性、透明细缝和边缘色污染均需校验；未通过审核的 sheet 及其成员保留在待审目录。

简单色键适合不透明卡通散件，不能处理与背景同色的前景或大面积半透明云雾。动画仍逐帧/逐层生成；组合静态图不替代连续帧。为项目新增组合模式时保留单件模板供定点补生成，不能按成员数量重复提交整张sheet。

## 命令

```bash
python3 scripts/asset_batch.py plan --manifest /absolute/path/assets.json
python3 scripts/asset_batch.py run --manifest /absolute/path/assets.json --output-dir /absolute/path/generated --max-new-tasks 4
```

manifest 格式按下方命令和脚本的 `--help` 创建；项目专属的资产范围、风格及验收标准应来自当前项目自身的资料。脚本要求 Python 3.11+ 和 Pillow；组合图拆分另需 NumPy。`--config` 仅供隔离测试或兼容配置使用，默认沿用当前 Codex 配置。脚本只处理静态 PNG/JPEG/WebP。输出目录含素材、状态文件和需要人工检查的原图；不要把生成目录误当成已验收资产目录。

## 插件路径与依赖

以当前已加载的 `SKILL.md` 所在目录为 Skill 根目录，解析本包 `scripts/`，不假设安装在 `~/.codex/skills`。示例命令均在该目录中执行或改为脚本绝对路径。不要修改插件缓存；状态、清单和产物写入用户项目或临时目录。

Python 3.11+；运行前检查 Pillow，组合图拆分还需 NumPy。缺失时向用户说明安装命令，由用户决定是否安装；不自动安装。推荐为该工具建独立 venv，执行 `python3 -m venv /absolute/path/venv`，再用其 Python 执行 `-m pip install -r /absolute/path/to/skill/requirements.txt`。

manifest 示例（资源范围、提示词和尺寸应来自当前项目）：

```json
{"defaults":{"model":"gpt-image-2.5-flare","size":"1024x1024","background":"transparent","output_format":"png","n":1},"assets":[{"id":"example_icon","prompt":"single clean game icon"}]}
```
