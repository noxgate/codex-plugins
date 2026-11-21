# NoxGate Codex 插件

本仓库维护 `noxgate-media`、`noxgate-game-assets` 的源码、安装包和 Codex 插件市场。NoxGate 网关和授权网页由 NoxGate 应用仓库维护。

## 安装与使用

选择与你的系统及 CPU 匹配的 ZIP。解压市场包后，将解压目录作为本地 Codex 插件市场添加。也可以直接添加 Git 市场 `noxgate/codex-plugins`。平台 ZIP 自带 Python，游戏资产包还包含 Pillow 与 NumPy。通过 Git 市场安装时，启动器会在首次使用时下载并校验相同的运行环境。运行环境保存在用户目录，后续启动会复用。

macOS 和 Linux 使用插件目录中的 `bin/noxgate.sh`，Windows 使用 `bin/noxgate.cmd`。可执行 `--help` 查看当前插件支持的命令，例如：

```bash
"/absolute/path/to/plugin/bin/noxgate.sh" auth ensure --capability image
"/absolute/path/to/plugin/bin/noxgate.sh" auth status
```

生成命令会自动检查授权。首次使用时，在本机浏览器登录 NoxGate、选择 API Key 并确认权限。两个插件默认使用同一用户级 profile。已有凭据按原配置复用，权限不足的旧配置可补授权一次。对同一凭据已经请求过但未获批准的权限，后续会提示变更授权。显式执行 `auth login` 可换绑 API Key 或重新选择权限。

通过 `NOXGATE_PROFILE` 或 `auth --profile` 选择不同的 profile。使用 `auth list` 查看已保存的 profile，`auth use work` 切换默认 profile，`auth logout` 删除本地凭据，`auth revoke` 同时撤销远端凭据。自建网关可指定 `--gateway` 和 `--web`。显式执行 `auth import-codex` 仅导入旧配置的网关地址，随后仍由用户在网页选择 API Key。

凭据默认保存在用户级私有文件中。macOS 使用 `~/Library/Application Support/NoxGate`，Linux 使用 `${XDG_CONFIG_HOME:-~/.config}/noxgate`，Windows 使用 `%LOCALAPPDATA%\NoxGate`（可回退到 `APPDATA`）。两个插件与独立 CLI 可用同一个绝对 `NOXGATE_HOME` 指向自定义位置。`--store system` 使用系统安全存储，`--store portable-env --env-file /absolute/path/.env` 使用指定的私有明文文件。`auth migrate-storage` 可显式迁移存储，默认保留源凭据。平台启动器内含 keyring 所需的 Python 依赖，系统后端仍需本机服务可用。原有 `python3 bin/install.py` 仍可安装独立 CLI。

在 Codex 使用 NoxGate 时，每次从技能菜单选中对应技能，或在请求中输入其调用名。通用图片、视频、音频分别使用 `$noxgate-imagegen`、`$noxgate-videogen`、`$noxgate-audiogen`，游戏图片素材和游戏音频分别使用 `$noxgate-image-assets`、`$noxgate-game-audio`。调用时会读取当前已绑定的 profile。

## 选择模型和查看成本

用户未指定模型时，先向用户展示当前 profile 可用的模型和费率，等用户选定后再生成。可按用途查询，用 `--count` 和 `--duration` 为可计算的费率估算基础费用。

```bash
"/absolute/path/to/plugin/bin/noxgate.sh" models --kind image --operation generate --count 3
"/absolute/path/to/plugin/bin/noxgate.sh" models --kind audio --operation music
"/absolute/path/to/plugin/bin/noxgate.sh" models --kind video --operation text-to-video --count 1 --duration 5
```

列表按当前 API Key 的模型权限筛选。报价采用官网基础费率，账户折扣、倍率和实际用量以结算为准。费率缺失的模型会保留查询结果并标明缺少价格。

## 任务恢复

任务状态文件保存任务 ID 和恢复索引。支持用 `resume --state` 查询已有异步任务或继续写入已缓存的产物。签名下载链接只在文件或系统引用存储中短期保留。`portable-env` 和 `env` 在首次同步下载失败且没有任务 ID 时无法恢复。旧状态可用 `noxgate tasks migrate-state --state /absolute/path/task.json --profile work` 显式迁移。旧游戏资产状态还需要 `--asset-root` 和 `--source-gateway`。

## 开发与打包

共享代码放在 `plugins/shared/`，插件专属代码放在各自目录。打包器根据 `plugins/catalog.json` 同步两份 `bin/`、`lib/` 副本，供 Codex 市场独立安装各插件。发布时更新对应的 `VERSION`、`plugin.json`、更新日志与市场版本。构建要求 Node.js 22.2+ 与 curl，用户机器通过平台包运行工具无需这些构建依赖。

```bash
node scripts/package-plugins.mjs                       # 当前平台
node scripts/package-plugins.mjs --platform linux-x64  # 指定平台
node scripts/package-plugins.mjs --all                 # 构建六种平台包
node scripts/package-plugins.mjs --source-only         # 适用于首次联网初始化的通用包
python3 -B -m unittest discover -s plugins/shared -p 'test_*.py' -v
node --test scripts/package-plugins.check.mjs
python3 -B scripts/check-platform-bundle.py           # 验证当前平台包
```

构建目录为 `dist/<platform>/` 或 `dist/source/`。每个目录都有两个插件 ZIP、一个 Codex 市场 ZIP、对应的 SHA-256 和 `manifest.json`。平台包支持 macOS 11+、Linux glibc 2.28+ 和 Windows 10/11 的 x64 与 arm64。构建所用的解释器和依赖版本、来源及校验值都在 `plugins/shared/runtime.lock`。授权回调的真实 Chromium 测试需要设置 `NOXGATE_TEST_BROWSER`。

现有官网单一下载入口可使用 `dist/source/` 的产物。从一次构建中取出 `manifest.json`、三个 ZIP 和 SHA-256 文件，更新 NoxGate 应用仓库的 `web-user/plugin-release/`。
