---
name: noxgate-videogen
description: 当用户希望生成视频、图生视频、使用参考视频或生成视频配音时，通过独立 NoxGate profile 的 视频接口实际生成视频文件。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户没有明确指定本次生成要用的模型时，先查询当前 profile 可用的模型。在对话中列出与用途匹配的模型名称、ID 和目录基础单价，展示能够按数量或时长计算的预估。即使只有一个候选，也等用户选定后再提交生成请求。用户点名时先核对权限和所需能力，沿用用户给出的模型 ID。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind video --operation text-to-video --count 1 --duration 5
```

按输入选择 `text-to-video`、`image-to-video` 或 `reference-to-video`。示例时长按用户需求调整。选定后将 ID 写入请求 JSON 的 `model`。 目录基础价以实际结算为准。费率或币种缺失时明确标注，能力待核实的模型需先确认支持该操作。根据查询结果展示选项，示例里的模型 ID 用来演示参数写法。恢复已有任务时使用任务原来的模型。


# NoxGate 视频生成

使用 `scripts/noxgate_video.py` 生成视频。创建任务使用 `POST /videos/generations`，异步结果通过 `GET /videos/generations/{task_id}` 轮询；不再需要的任务可以取消。模型、比例、分辨率、时长和参考媒体 URL 只传用户要求且模型支持的字段。

先将请求写入临时 JSON（不要写入凭据），再执行：

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" video generate \
  --request /tmp/noxgate-video-request.json \
  --output /tmp/noxgate-video.mp4
```

请求至少包含 `model`，并包含 `prompt`、`image_url`、`image_urls` 或 `media` 之一。脚本会读取独立 NoxGate profile，保存 `<output>.task.json`，处理同步或异步响应，下载结果并校验常见 MP4/WebM 容器。失败或超时后使用 `resume --state <state>`，不要自动重新提交。

提交前按 NoxGate 模型能力说明核对所用字段。不要未经用户同意将所需时长、带声音视频或参考媒体降级为另一种结果。参考输入必须是上游可访问的 HTTPS URL；本地文件不能当 URL，未经授权不要上传到第三方托管。

完成后检查文件存在，使用绝对路径展示视频/音频。提交结果未知时报告实际状态；任务查询不自动重复生成。`status --task-id ID` 只查询。`cancel --task-id ID` 仅在用户取消该任务时使用；音频和图片暂无公开取消端点。
