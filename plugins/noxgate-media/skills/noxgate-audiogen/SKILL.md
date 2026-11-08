---
name: noxgate-audiogen
description: 当用户希望生成音乐、战斗音乐、音效、环境音或语音时，通过当前 Codex provider 的 NoxGate 音频接口实际生成音频文件。
---

# NoxGate 音频生成

使用 `scripts/noxgate_audio.py` 生成音频。网关入口是 `POST /audio/speech`，既可能直接返回音频二进制，也可能返回异步 JSON 任务；异步结果使用 `GET /audio/speech/{task_id}` 查询。Suno 和 Seed Audio 等模型需要 `prompt`，传统 TTS 需要 `input`，两者只能选择一个。

先将请求写入临时 JSON（不要写入凭据），再执行：

```bash
# 将此处替换为当前加载的 SKILL.md 所在目录
SKILL_DIR="/absolute/path/to/noxgate-audiogen"
python3 "$SKILL_DIR/scripts/noxgate_audio.py" generate \
  --request /tmp/noxgate-audio-request.json \
  --output /tmp/noxgate-audio.mp3
```

请求至少包含 `model` 和 `input` 或 `prompt`。可按模型支持情况提供 `voice`、`format`、`sample_rate`、`instrumental`、`style`、`title`、`duration`、`negative_tags` 等字段。脚本读取当前 Codex provider、保存任务状态、处理异步轮询并下载结果；超时后使用 `resume --state`，不要重复付费提交。

模型应从 `models` 命令返回的当前账户模型 ID 和 NoxGate 模型能力说明中选择；可见不代表支持所有字段。不要未经用户同意将所需时长、带声音视频或参考媒体降级为另一种结果。参考输入必须是上游可访问的 HTTPS URL；本地文件不能当 URL，未经授权不要上传到第三方托管。

完成后检查文件存在，使用绝对路径展示视频/音频。提交结果未知时报告实际状态；任务查询不自动重复生成。`status --task-id ID` 只查询。音频暂无公开取消端点；停止本地轮询不会取消上游任务。
