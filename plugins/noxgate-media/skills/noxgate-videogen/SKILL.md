---
name: noxgate-videogen
description: 当用户希望生成视频、图生视频、使用参考视频或生成视频配音时，通过独立 NoxGate profile 的 视频接口实际生成视频文件。
---

## 启动与认证

从本文件向上两级得到插件根目录，并将以下命令中的 `PLUGIN_ROOT` 设为该绝对路径。macOS/Linux 使用 `"$PLUGIN_ROOT/bin/noxgate.sh"`，Windows 使用同目录的 `noxgate.cmd`，后面的命令和参数相同。平台 ZIP 自带运行环境。通过 Git 市场安装时，启动器会在首次使用时下载并校验所需的运行环境，后续共用本机缓存。

生成命令会自动检查认证。首次使用时在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认共用同一用户级 profile，权限满足且凭据有效时，换会话也会沿用。用 `auth status` 检查当前授权，`auth ensure` 用于初始化或补充授权，`auth login` 用于主动更换 API Key 或权限。可用 `NOXGATE_PROFILE` 或 `auth --profile` 选择独立 profile。

凭据由运行时读取。请勿从配置目录、钥匙串或请求日志提取秘密，也不要把认证头写入请求文件。默认使用私有文件存储，支持显式选择 `--store system` 或 `--store portable-env --env-file /absolute/path/to/plugin/.env`。便携模式以明文保存插件凭据。


## 选择模型和查看成本

用户未指定本次生成的模型时，按实际用途查询当前 profile 的授权模型。用户指定了模型时，直接从完整的 `models` 中核对该 ID 的权限和用途。恢复任务时沿用任务原来的模型。

```bash
"$PLUGIN_ROOT/bin/noxgate.sh" models --kind video --operation text-to-video --count 1 --duration 5
```

按输入选择 `text-to-video`、`image-to-video` 或 `reference-to-video`。示例时长按用户需求调整。选定后将 ID 写入请求 JSON 的 `model`。

查询结果的 `latest_by_family` 优先展示能力已确认的各系列最新候选 ID。仅有能力待核实的型号时，明确标注其状态。按这些 ID 从 `models` 读取名称、目录基础费率和可计算的本次预估费用。显示各系列及其候选，保留同版本的多个型号。费率缺失时标明「暂无报价」，选中能力待核实的型号后，先核实兼容性再生成。没有适用型号时，说明查询结果并结束选型。

宿主提供可点击的用户输入工具时，优先调用该工具，例如 `request_user_input_async`。用「请选择本次生成的模型」提问，每个选项显示型号和费用，并映射到查询结果中的准确模型 ID。超过工具的选项容量时按系列分组或分页，让用户能访问所有候选。完整的 `models` 中还有其他适用型号时，提供「查看全部模型」选项，进入完整列表后继续提供可点击选择。宿主没有可用的选项工具时，展示带编号的同一批候选和费用，接受编号或模型 ID。

即使只有一个候选，也等用户明确选择后再提交生成。选中分组、翻页或查看全部时继续展示选项。费用按目录基础价估算，实际费用以结算为准。


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
