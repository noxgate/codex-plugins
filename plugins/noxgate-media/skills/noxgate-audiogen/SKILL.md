---
name: noxgate-audiogen
description: 当用户希望生成音乐、战斗音乐、音效、环境音或语音时，通过独立 NoxGate profile 的 音频接口实际生成音频文件。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户没有明确指定本次生成要用的模型时，先查询当前 profile 可用的模型。在对话中列出与用途匹配的模型名称、ID 和目录基础单价，展示能够按数量或时长计算的预估。即使只有一个候选，也等用户选定后再提交生成请求。用户点名时先核对权限和所需能力，沿用用户给出的模型 ID。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind audio --operation music --count 1
```

音乐用 `music`，音效或环境音用 `sound`，文字转语音用 `tts`。选定后将 ID 写入请求 JSON 的 `model`。 目录基础价以实际结算为准。费率或币种缺失时明确标注，能力待核实的模型需先确认支持该操作。根据查询结果展示选项，示例里的模型 ID 用来演示参数写法。恢复已有任务时使用任务原来的模型。


# NoxGate 音频生成

使用 `scripts/noxgate_audio.py` 生成音频。网关入口是 `POST /audio/speech`，既可能直接返回音频二进制，也可能返回异步 JSON 任务；异步结果使用 `GET /audio/speech/{task_id}` 查询。Suno 和 Seed Audio 等模型需要 `prompt`，传统 TTS 需要 `input`，两者只能选择一个。

先将请求写入临时 JSON（不要写入凭据），再执行：

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" audio generate \
  --request /tmp/noxgate-audio-request.json \
  --output /tmp/noxgate-audio.mp3
```

请求至少包含 `model` 和 `input` 或 `prompt`。可按模型支持情况提供 `voice`、`format`、`sample_rate`、`instrumental`、`style`、`title`、`duration`、`negative_tags` 等字段。脚本读取独立 NoxGate profile、保存任务状态、处理异步轮询并下载结果；超时后使用 `resume --state`，不要重复付费提交。

提交前按 NoxGate 模型能力说明核对所用字段。不要未经用户同意将所需时长、带声音视频或参考媒体降级为另一种结果。参考输入必须是上游可访问的 HTTPS URL；本地文件不能当 URL，未经授权不要上传到第三方托管。

完成后检查文件存在，使用绝对路径展示视频/音频。提交结果未知时报告实际状态；任务查询不自动重复生成。`status --task-id ID` 只查询。音频暂无公开取消端点；停止本地轮询不会取消上游任务。
