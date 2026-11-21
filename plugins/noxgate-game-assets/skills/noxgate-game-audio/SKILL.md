---
name: noxgate-game-audio
description: 当用户要为游戏批量生成 BGM、战斗音乐、菜单音乐、音效或环境音时，通过独立 NoxGate profile 的 音频接口生产并验收音频资产。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户没有明确指定本次生成要用的模型时，先查询当前 profile 可用的模型。在对话中列出与用途匹配的模型名称、ID 和目录基础单价，展示能够按数量或时长计算的预估。即使只有一个候选，也等用户选定后再提交生成请求。用户点名时先核对权限和所需能力，沿用用户给出的模型 ID。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind audio --operation sound --count 1
```

分别按 `music` 和 `sound` 查询音乐及音效，数量使用各自计划提交的任务数。把用户选定的 ID 写入对应资产的 `model`。 目录基础价以实际结算为准。费率或币种缺失时明确标注，能力待核实的模型需先确认支持该操作。根据查询结果展示选项，示例里的模型 ID 用来演示参数写法。恢复已有任务时使用任务原来的模型。


# NoxGate 游戏音频

使用 JSON manifest 管理有稳定 ID 的音频资产。每项至少包含 `id`、`model`、`prompt`；可选 `output`、`format`、`instrumental`、`style`、`title`、`duration`、`negative_tags` 等模型支持字段。

示例：

```json
{"assets":[
  {"id":"menu_theme","model":"suno-v6-beta","prompt":"warm strategic menu theme, loopable, no vocals","instrumental":true,"output":"menu_theme.mp3"},
  {"id":"sword_hit","model":"doubao-seed-audio-1-0","prompt":"short metallic sword impact, clean transient","output":"sword_hit.wav"}
]}
```

先检查清单和数量，再执行批次脚本：

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" audio-batch plan --manifest audio.json
"$PLUGIN_ROOT/bin/noxgate.sh" audio-batch run --manifest audio.json --output-dir ./audio-output --max-new-tasks 2
```

脚本会从独立 NoxGate profile 读取认证，使用 `/audio/speech` 创建任务，轮询 `/audio/speech/{task_id}`，保存每个资产的 `.task.json`，下载并检查常见音频格式。提交结果未知时只使用 `resume`，不要自动重提；完成后人工检查响度、循环点、噪声、时长、命名和目录。
