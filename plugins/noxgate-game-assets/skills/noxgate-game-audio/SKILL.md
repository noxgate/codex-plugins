---
name: noxgate-game-audio
description: 当用户要为游戏批量生成 BGM、战斗音乐、菜单音乐、音效或环境音时，通过当前 Codex provider 的 NoxGate 音频接口生产并验收音频资产。
---

# NoxGate 游戏音频

使用 JSON manifest 管理有稳定 ID 的音频资产。每项至少包含 `id`、`model`、`prompt`；可选 `output`、`format`、`instrumental`、`style`、`title`、`duration`、`negative_tags` 等模型支持字段。

示例：

```json
{"assets":[
  {"id":"menu_theme","model":"suno-v6-beta","prompt":"warm strategic menu theme, loopable, no vocals","instrumental":true,"output":"menu_theme.mp3"},
  {"id":"sword_hit","model":"doubao-seed-audio-2.0","prompt":"short metallic sword impact, clean transient","output":"sword_hit.wav"}
]}
```

先检查清单和数量，再执行批次脚本：

```bash
# 将此处替换为当前加载的 SKILL.md 所在目录，不假设用户级 Skill 路径
SKILL_DIR="/absolute/path/to/noxgate-game-audio"
python3 "$SKILL_DIR/scripts/batch_audio.py" plan --manifest audio.json
python3 "$SKILL_DIR/scripts/batch_audio.py" run --manifest audio.json --output-dir ./audio-output --max-new-tasks 2
```

脚本会从当前 Codex provider 读取认证，使用 `/audio/speech` 创建任务，轮询 `/audio/speech/{task_id}`，保存每个资产的 `.task.json`，下载并检查常见音频格式。提交结果未知时只使用 `resume`，不要自动重提；完成后人工检查响度、循环点、噪声、时长、命名和目录。
