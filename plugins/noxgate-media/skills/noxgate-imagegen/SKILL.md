---
name: noxgate-imagegen
description: 必须用于任何用户希望生成、绘制、创作、重绘或编辑图片的请求，包括“画一下”“生成一张图”“做张插画/头像/海报/游戏地图/图标”以及基于参考图修改。优先通过当前 Codex 配置调用 NoxGate GPT Image 2.5；不要用 Python、SVG、Canvas 或代码绘图替代用户要求的图片生成。仅当用户明确要求程序化绘图、制作图表/示意图、实现游戏画面或写绘图代码时，才按编程任务处理。
---

# NoxGate 图片生成

当请求的主要结果是图片文件时，直接调用本 skill 的脚本生成图片。不要只给提示词、示例代码或绘图库结果。图片编辑请求使用 `edit`，提供参考图；其他图像创作请求使用 `generate`。即使请求出现在游戏或代码项目中，只要用户要的是图片文件，也调用图片接口；用户要求把素材批量纳入游戏资产流水线且已安装游戏资产插件时，让 `gpt-image-2-5-game-assets` 负责清单、批次和验收，它的生成脚本同样使用 Codex provider 认证。

## 多素材的生成方式

提示词明确要求多个素材或多张图片时，生成前主动评估是否适合组合成一张源图，不必等用户明确说“放在一张图里”。同类游戏素材、成套图标、配色或状态变体，以及其他明显可同图排布的内容，若风格、视角和背景要求兼容，每项都能保留足够细节且可清晰分区，应考虑同图生成；数量较多时按适合的同类小组拆成多张组合图。

独立场景或完整构图、不同尺寸或风格、高细节大图、连续动画帧，以及合图后会混淆、重叠或无法可靠拆分的内容，应分别生成。混合请求只合并适合的部分，其余单独生成；用户明确要求逐张独立生成时遵从该要求。

组合图提示词要列出成员与位置，每格一件、轮廓互不接触、留足边距，不加文字标签或跨格阴影。区分源图张数与最终素材数，例如 6 个图标可用一张源图生成，不要为每个成员重复请求整张组合图。生成前简要说明分组方式；最终按用户要求交付整图或拆分后的独立文件，并逐件检查完整性、清晰度和数量。若无法可靠拆分并满足独立文件的交付要求，应改用逐件生成；不要仅交付一张组合图冒充多个独立素材，也不要为凑组合图添加用户未要求的成员。

## 认证与模型

- 脚本从 `$CODEX_HOME/config.toml`（未设置时 `~/.codex/config.toml`）读取 `model_provider`，再读取对应 `[model_providers.<name>]` 的 `base_url`。
- 认证优先使用 provider 的 `experimental_bearer_token`；若该字段为空，则按 `env_key` 从环境变量读取。不要显示、复制、记录或写入图片元数据中的 token。
- 默认模型为 `gpt-image-2.5-flare`。需要更高质量、角色一致性或参考图编辑时使用 `gpt-image-2.5-sunburst`。它们是本包推荐的默认模型；用户指定其他模型时，先核对当前 provider 的模型权限与参数。
- 使用非流式请求：生成向 `POST /images/generations` 发送 JSON，编辑向 `POST /images/edits` 发送 multipart。脚本优先请求 `b64_json`，也能保存 URL 返回的图片，并支持异步任务轮询。要求 Python 3.11+。
- 按上述分组方式提交所需的源图张数，单图请求默认一张，最终素材数量仍按用户要求。提交超时或结果不明时，报告错误，不自动重新提交付费请求。

## 生成

调用脚本时将用户意图整理为清楚、具体的视觉提示词。普通单图默认 PNG、`1024x1024`、`medium` 质量；用户明确指定尺寸、比例、透明背景或质量时按需传参。若未指定本地保存位置，使用系统临时目录下不冲突的文件名，避免向项目仓库写入临时产物。

```bash
# 使用当前已加载的 SKILL.md 所在目录，不假设用户级 Skill 安装路径
SKILL_DIR="/absolute/path/to/noxgate-imagegen"
python3 "$SKILL_DIR/scripts/noxgate_image.py" generate \
  --prompt "描述图片内容" \
  --output /tmp/codex-generated-image.png
```

## 编辑

用户提供本地参考图时使用 `edit`，将每张图片作为 `--image` 传入。可选 `--mask` 指定遮罩；脚本通过 multipart `image[]` 上传，不把本地路径伪装成远程 URL。

```bash
# 使用当前已加载的 SKILL.md 所在目录，不假设用户级 Skill 安装路径
SKILL_DIR="/absolute/path/to/noxgate-imagegen"
python3 "$SKILL_DIR/scripts/noxgate_image.py" edit \
  --prompt "保留构图，将杯子改成红色" \
  --image /absolute/path/reference.png \
  --output /tmp/codex-edited-image.png
```

## 返回结果

请求成功后确认脚本输出路径和图片文件存在，并在回复中用该绝对路径展示结果，例如 `![生成结果](/tmp/codex-generated-image.png)`。失败时报告实际接口状态或错误；不要静默改用绘图库伪装成 AI 生图。只有用户接着要求修改生成代码或将图片嵌入项目时，才编辑项目文件。

## 插件中的路径和恢复

从当前加载的 `SKILL.md` 位置解析 `scripts/`；不要假设插件的缓存目录。脚本从相对插件路径载入内置运行库，支持包含空格的安装路径。用户配置保持在 `$CODEX_HOME`，清单、状态和产物放在项目或临时目录。

每次生成或编辑都会保存 `<output>.task.json`。超时、下载失败时用 `resume --state /absolute/path/output.png.task.json` 恢复同一任务；提交结果未知时先检查网关记录，不删除状态后自动重生。
