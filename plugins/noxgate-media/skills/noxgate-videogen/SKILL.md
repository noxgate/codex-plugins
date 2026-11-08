---
name: noxgate-videogen
description: 当用户希望生成视频、图生视频、使用参考视频或生成视频配音时，通过当前 Codex provider 的 NoxGate 视频接口实际生成视频文件。
---

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

请求至少包含 `model`，并包含 `prompt`、`image_url`、`image_urls` 或 `media` 之一。脚本会读取当前 Codex provider，保存 `<output>.task.json`，处理同步或异步响应，下载结果并校验常见 MP4/WebM 容器。失败或超时后使用 `resume --state <state>`，不要自动重新提交。

模型应从 `models` 命令返回的当前账户模型 ID 和 NoxGate 模型能力说明中选择；可见不代表支持所有字段。不要未经用户同意将所需时长、带声音视频或参考媒体降级为另一种结果。参考输入必须是上游可访问的 HTTPS URL；本地文件不能当 URL，未经授权不要上传到第三方托管。

完成后检查文件存在，使用绝对路径展示视频/音频。提交结果未知时报告实际状态；任务查询不自动重复生成。`status --task-id ID` 只查询。`cancel --task-id ID` 仅在用户取消该任务时使用；音频和图片暂无公开取消端点。
