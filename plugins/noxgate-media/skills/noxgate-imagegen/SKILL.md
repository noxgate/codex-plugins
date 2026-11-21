---
name: noxgate-imagegen
description: 用户选用此技能时，通过已绑定的 NoxGate API Key 生成或编辑图片，如插画、头像、海报、游戏地图和单个图标。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户没有明确指定本次生成要用的模型时，先查询当前 profile 可用的模型。在对话中列出与用途匹配的模型名称、ID 和目录基础单价，展示能够按数量或时长计算的预估。即使只有一个候选，也等用户选定后再提交生成请求。用户点名时先核对权限和所需能力，沿用用户给出的模型 ID。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind image --operation generate --count 1
```

参考图编辑改用 `--operation edit`。在执行生成命令时通过 `--model` 传入用户选定的 ID。 目录基础价以实际结算为准。费率或币种缺失时明确标注，能力待核实的模型需先确认支持该操作。根据查询结果展示选项，示例里的模型 ID 用来演示参数写法。恢复已有任务时使用任务原来的模型。


# NoxGate 图片生成

用户选用本技能后，按上面的模型选择流程调用脚本生成图片。编辑参考图时使用 `edit`，其他图像创作使用 `generate`。如果用户需要批量处理游戏素材清单，可提示他们显式选择 `$noxgate-image-assets`，由该技能负责批次和验收。

## 多素材的生成方式

提示词明确要求多个素材或多张图片时，生成前主动评估是否适合组合成一张源图，不必等用户明确说“放在一张图里”。同类游戏素材、成套图标、配色或状态变体，以及其他明显可同图排布的内容，若风格、视角和背景要求兼容，每项都能保留足够细节且可清晰分区，应考虑同图生成；数量较多时按适合的同类小组拆成多张组合图。

独立场景或完整构图、不同尺寸或风格、高细节大图、连续动画帧，以及合图后会混淆、重叠或无法可靠拆分的内容，应分别生成。混合请求只合并适合的部分，其余单独生成；用户明确要求逐张独立生成时遵从该要求。

组合图提示词要列出成员与位置，每格一件、轮廓互不接触、留足边距，不加文字标签或跨格阴影。区分源图张数与最终素材数，例如 6 个图标可用一张源图生成，不要为每个成员重复请求整张组合图。生成前简要说明分组方式；最终按用户要求交付整图或拆分后的独立文件，并逐件检查完整性、清晰度和数量。若无法可靠拆分并满足独立文件的交付要求，应改用逐件生成；不要仅交付一张组合图冒充多个独立素材，也不要为凑组合图添加用户未要求的成员。

## 认证与模型

- 图片脚本通过共享认证运行时读取当前 NoxGate profile，在进程内获得独立插件凭据。首次调用自动打开浏览器授权。
- 仅显式运行 noxgate auth import-codex 才读取旧配置中的公开网关地址；不会复制旧 API Key，仍须在网页重新选择 Key 授权。
- 可为草稿推荐 `gpt-image-2.5-flare`，为定稿或参考图编辑推荐 `gpt-image-2.5-sunburst`。推荐前查询当前 API Key 的可用模型和价格。
- 使用非流式请求：生成向 `POST /images/generations` 发送 JSON，编辑向 `POST /images/edits` 发送 multipart。脚本优先请求 `b64_json`，也能保存 URL 返回的图片，并支持异步任务轮询。
- 按上述分组方式提交所需的源图张数，单图请求默认一张，最终素材数量仍按用户要求。提交超时或结果不明时，报告错误，不自动重新提交付费请求。

## 生成

调用脚本时将用户意图整理为清楚、具体的视觉提示词。普通单图默认 PNG、`1024x1024`、`medium` 质量；用户明确指定尺寸、比例、透明背景或质量时按需传参。若未指定本地保存位置，使用系统临时目录下不冲突的文件名，避免向项目仓库写入临时产物。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" image generate \
  --model gpt-image-2.5-flare \
  --prompt "描述图片内容" \
  --output /tmp/codex-generated-image.png
```

## 编辑

用户提供本地参考图时使用 `edit`，将每张图片作为 `--image` 传入。可选 `--mask` 指定遮罩；脚本通过 multipart `image[]` 上传，不把本地路径伪装成远程 URL。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" image edit \
  --model gpt-image-2.5-sunburst \
  --prompt "保留构图，将杯子改成红色" \
  --image /absolute/path/reference.png \
  --output /tmp/codex-edited-image.png
```

## 返回结果

请求成功后确认脚本输出路径和图片文件存在，并在回复中用该绝对路径展示结果，例如 `![生成结果](/tmp/codex-generated-image.png)`。失败时报告实际接口状态或错误；不要静默改用绘图库伪装成 AI 生图。只有用户接着要求修改生成代码或将图片嵌入项目时，才编辑项目文件。

## 插件中的路径和恢复

从当前加载的 `SKILL.md` 位置解析启动器路径，引用路径时保留引号。profile 配置和默认私有文件凭据位于跨平台用户配置目录，可由 `NOXGATE_HOME` 覆盖；也可显式选择系统安全存储或 `portable-env`。清单、状态和产物放在项目或临时目录。

每次生成或编辑都会保存 `<output>.task.json`。超时、下载失败时用 `resume --state /absolute/path/output.png.task.json` 恢复同一任务；提交结果未知时先检查网关记录，不删除状态后自动重生。
