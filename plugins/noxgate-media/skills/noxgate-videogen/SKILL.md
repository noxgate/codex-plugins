---
name: noxgate-videogen
description: 当用户希望生成视频、图生视频、使用参考视频或生成视频配音时，通过独立 NoxGate profile 的 视频接口实际生成视频文件。
---

## 独立认证（首次使用必须执行）

从当前 SKILL.md 向上两级得到插件根目录。首次安装独立 CLI；默认 `file` 后端无需额外认证依赖：

```bash
python3 "/absolute/path/to/plugin/bin/install.py"
python3 "/absolute/path/to/plugin/bin/noxgate" auth ensure --capability video
```

后续可使用 noxgate auth status、list、use work、logout（删除本地凭据）、revoke（撤销远端并删除本地）。通过 NOXGATE_PROFILE 或 auth --profile 选择 profile。首次授权打开 NoxGate 登录页，用户在网页选择或创建 API Key，运行时自动安全保存凭据。

首次授权必须在本机打开浏览器并登录 NoxGate，选择自己的 API Key 后批准；CLI 自动接收授权结果并保存独立插件凭据。无本机浏览器时，请稍后在可打开浏览器的本机运行 `noxgate auth login`；没有设备码、手工代码或服务端轮询流程。禁止 Agent 读取配置目录、钥匙串、原始密钥或授权秘密，禁止把认证头写入请求文件。默认凭据保存在用户配置目录私有 `file` 后端；只有显式选择 `--store system` 才使用系统安全存储，并需先安装 `--system-keyring` 依赖。`portable-env` 仅接受用户指定的绝对 `.env` 路径，以明文保存插件凭据并在 status 中警告。WorkBuddy 等 Agent 尚未完成真实宿主验证，不宣称已适配。



# NoxGate 视频生成

使用 `scripts/noxgate_video.py` 生成视频。创建任务使用 `POST /videos/generations`，异步结果通过 `GET /videos/generations/{task_id}` 轮询；不再需要的任务可以取消。模型、比例、分辨率、时长和参考媒体 URL 只传用户要求且模型支持的字段。

先将请求写入临时 JSON（不要写入凭据），再执行：

```bash
# 将此处替换为当前加载的 SKILL.md 所在目录
SKILL_DIR="/absolute/path/to/noxgate-videogen"
python3 "$SKILL_DIR/scripts/noxgate_video.py" generate \
  --request /tmp/noxgate-video-request.json \
  --output /tmp/noxgate-video.mp4
```

请求至少包含 `model`，并包含 `prompt`、`image_url`、`image_urls` 或 `media` 之一。脚本会读取独立 NoxGate profile，保存 `<output>.task.json`，处理同步或异步响应，下载结果并校验常见 MP4/WebM 容器。失败或超时后使用 `resume --state <state>`，不要自动重新提交。

模型应从 `models` 命令返回的当前账户模型 ID 和 NoxGate 模型能力说明中选择；可见不代表支持所有字段。不要未经用户同意将所需时长、带声音视频或参考媒体降级为另一种结果。参考输入必须是上游可访问的 HTTPS URL；本地文件不能当 URL，未经授权不要上传到第三方托管。

完成后检查文件存在，使用绝对路径展示视频/音频。提交结果未知时报告实际状态；任务查询不自动重复生成。`status --task-id ID` 只查询。`cancel --task-id ID` 仅在用户取消该任务时使用；音频和图片暂无公开取消端点。
