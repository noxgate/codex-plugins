---
name: noxgate-image-assets
description: 使用当前 NoxGate API Key 可调用的图片生成模型生产游戏美术，支持批量生成、组合图拆分、色键去背景、资产校验、断点续跑和序列帧图集组装。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户未指定本次生成的模型时，按实际用途查询当前 profile 的授权模型。用户指定了模型时，直接从完整的 `models` 中核对该 ID 的权限和用途。恢复任务时沿用任务原来的模型。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind game-image --operation generate --count 1
```

将数量换成计划提交的源图总数。对组合图按源图计费。把选定的 ID 写入清单的 `defaults.model` 或对应资产的 `model`。

查询结果的 `latest_by_family` 优先展示能力已确认的各系列最新候选 ID。仅有能力待核实的型号时，明确标注其状态。按这些 ID 从 `models` 读取名称、目录基础费率和可计算的本次预估费用。显示各系列及其候选，保留同版本的多个型号。费率缺失时标明「暂无报价」，选中能力待核实的型号后，先核实兼容性再生成。没有适用型号时，说明查询结果并结束选型。

宿主提供可点击的用户输入工具时，优先调用该工具，例如 `request_user_input_async`。用「请选择本次生成的模型」提问，每个选项显示型号和费用，并映射到查询结果中的准确模型 ID。超过工具的选项容量时按系列分组或分页，让用户能访问所有候选。完整的 `models` 中还有其他适用型号时，提供「查看全部模型」选项，进入完整列表后继续提供可点击选择。宿主没有可用的选项工具时，展示带编号的同一批候选和费用，接受编号或模型 ID。

即使只有一个候选，也等用户明确选择后再提交生成。选中分组、翻页或查看全部时继续展示选项。费用按目录基础价估算，实际费用以结算为准。


# NoxGate 图片素材

在项目中生产素材时，先读取该项目的资产清单、视觉规范与验收要求；不要把其他项目的素材、约定或参考资料带入当前项目。本 skill 本身只包含通用工作流。

## 模型与接口

- 可选模型取决于当前 API Key 的权限。把查询结果里的完整模型 ID 写入清单。模型显示「能力待核实」时，先查看该模型需要哪些参考输入和参数。
- 按所选模型的能力填写 `size`、`resolution`、`quality`、`background`、`output_format` 和 `n`。首次使用新模型时，可省略其可选参数以使用网关默认值。游戏素材默认验收透明 PNG，纯色背景组合图可显式设置 `background: opaque`。
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
"$PLUGIN_ROOT/bin/noxgate.sh" frame-batch plan --manifest /absolute/path/frames.json
"$PLUGIN_ROOT/bin/noxgate.sh" frame-batch assemble --manifest /absolute/path/frames.json --output-dir /absolute/path/atlases
"$PLUGIN_ROOT/bin/noxgate.sh" frame-batch validate --manifest /absolute/path/frames.json
```

`assemble` 可重复执行，输出以临时文件替换，适合无人值守断点任务。源帧若尺寸、透明度或数量不合格会失败，不自动裁切或改变锚点。用 `asset-batch` 和当前 API Key 可用的模型生成源帧。

## 同类组合图工作流

提示词明确要求多个素材或多张图片时，建立 manifest 前主动评估是否适合组合成一张源图，不必等用户明确说“放在一张图里”。同类游戏素材、成套图标、配色或状态变体，以及其他明显可同图排布的内容，若风格、视角和背景要求兼容，每项都能保留足够细节且可清晰分区，应考虑同图生成；数量较多时按适合的同类小组拆成多张组合图。

独立场景或完整构图、不同尺寸或风格、高细节大图、连续动画帧，以及合图后会混淆、重叠或无法可靠拆分的内容，应分别生成。混合清单只合并适合的部分，其余单独生成；用户明确要求逐张独立生成时遵从该要求。

确定使用组合图后，用一套提示词生成纯色背景的多素材图，统一风格后用 `scripts/sheet_batch.py` 拆成独立 PNG。先读取 [references/sheets.md](references/sheets.md) 中的分区 manifest、去背景适用范围和命令。生成前简要说明分组方式，区分源图张数与最终素材数。最终按用户要求交付整图或拆分后的独立文件，不要仅交付一张组合图冒充多个独立素材，也不要为凑组合图添加用户未要求的成员。固定每格的成员 ID，不依赖模型输出文字标注。框外留白、分区完整性、透明细缝和边缘色污染均需校验；未通过审核的 sheet 及其成员保留在待审目录。

简单色键适合不透明卡通散件，不能处理与背景同色的前景或大面积半透明云雾。动画仍逐帧/逐层生成；组合静态图不替代连续帧。为项目新增组合模式时保留单件模板供定点补生成，不能按成员数量重复提交整张sheet。

## 命令

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" asset-batch plan --manifest /absolute/path/assets.json
"$PLUGIN_ROOT/bin/noxgate.sh" asset-batch run --manifest /absolute/path/assets.json --output-dir /absolute/path/generated --max-new-tasks 4
```

manifest 格式按下方命令和脚本的 `--help` 创建；项目专属的资产范围、风格及验收标准应来自当前项目自身的资料。启动器负责加载包内 Python、Pillow 和 NumPy。使用 --profile 选择独立 profile；不再支持 --config 或静默 Codex 回退。脚本只处理静态 PNG/JPEG/WebP。输出目录含素材、状态文件和需要人工检查的原图；不要把生成目录误当成已验收资产目录。

## 插件路径与依赖

通过插件启动器执行示例，状态、清单和产物写入用户项目或临时目录。

manifest 示例（资源范围、提示词和尺寸应来自当前项目）：

```json
{"defaults":{"model":"<用户选定的模型 ID>","n":1},"assets":[{"id":"example_icon","prompt":"single clean game icon"}]}
```
