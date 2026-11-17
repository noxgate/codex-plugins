---
name: noxgate-game-audio
description: 当用户要为游戏批量生成 BGM、战斗音乐、菜单音乐、音效或环境音时，通过独立 NoxGate profile 的 音频接口生产并验收音频资产。
---

## 独立认证（首次使用必须执行）

从当前 SKILL.md 向上两级得到插件根目录。首次安装独立 CLI 和游戏图像依赖；默认 `file` 后端无需额外认证依赖：

```bash
python3 "/absolute/path/to/plugin/bin/install.py" --game-assets
python3 "/absolute/path/to/plugin/bin/noxgate" auth ensure --capability audio
```

后续可使用 noxgate auth status、list、use work、logout（删除本地凭据）、revoke（撤销远端并删除本地）。通过 NOXGATE_PROFILE 或 auth --profile 选择 profile。首次授权打开 NoxGate 登录页，用户在网页选择或创建 API Key，运行时自动安全保存凭据。

首次授权必须在本机打开浏览器并登录 NoxGate，选择自己的 API Key 后批准；CLI 自动接收授权结果并保存独立插件凭据。无本机浏览器时，请稍后在可打开浏览器的本机运行 `noxgate auth login`；没有设备码、手工代码或服务端轮询流程。禁止 Agent 读取配置目录、钥匙串、原始密钥或授权秘密，禁止把认证头写入请求文件。默认凭据保存在用户配置目录私有 `file` 后端；只有显式选择 `--store system` 才使用系统安全存储，并需先安装 `--system-keyring` 依赖。`portable-env` 仅接受用户指定的绝对 `.env` 路径，以明文保存插件凭据并在 status 中警告。WorkBuddy 等 Agent 尚未完成真实宿主验证，不宣称已适配。



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
# 将此处替换为当前加载的 SKILL.md 所在目录，不假设用户级 Skill 路径
SKILL_DIR="/absolute/path/to/noxgate-game-audio"
python3 "$SKILL_DIR/scripts/batch_audio.py" plan --manifest audio.json
python3 "$SKILL_DIR/scripts/batch_audio.py" run --manifest audio.json --output-dir ./audio-output --max-new-tasks 2
```

脚本会从独立 NoxGate profile 读取认证，使用 `/audio/speech` 创建任务，轮询 `/audio/speech/{task_id}`，保存每个资产的 `.task.json`，下载并检查常见音频格式。提交结果未知时只使用 `resume`，不要自动重提；完成后人工检查响度、循环点、噪声、时长、命名和目录。
