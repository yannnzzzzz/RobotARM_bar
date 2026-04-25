# 智能自助调酒系统机械臂桥接

## 启动

在仓库根目录执行：

```bash
python3 Software/Web/robot_bridge.py
```

然后打开：

```text
http://127.0.0.1:8765/index.html
```

## MomoTender 动态推荐

`MomoTender` 是第五张动态酒单卡片。点击后会：

1. 读取 `Software/ai/runs/emotion/latest.json` 的最新情绪识别结果。
2. 使用根目录 `.env` 中的兼容 OpenAI 接口配置请求 LLM。
3. 只在固定点位 `01-07` 内生成 2 到 3 个原料点位的随机配方。
4. 如果接口超时、网络波动或返回内容不合法，则自动回退到本地固定策略。

当前动态酒单占位图是：

```text
Software/Web/pic/mysterious.png
```

## 环境变量

先复制模板：

```bash
cp .env.example .env
```

然后配置下面这些变量：

```text
MOMOTENDER_API_BASE_URL
MOMOTENDER_API_KEY
MOMOTENDER_API_MODEL
MOMOTENDER_API_TIMEOUT_SECONDS
MOMOTENDER_API_MAX_OUTPUT_TOKENS
MOMOTENDER_API_TEMPERATURE
MOMOTENDER_EMOTION_JSON
```

`.env` 已在仓库根目录 `.gitignore` 中忽略，不会进入版本控制。

## 本地接口

页面目前会用到三个本地接口：

```text
GET  /api/status
POST /api/run
POST /api/momotender/recommend
```

`/api/momotender/recommend` 会返回适合前端直接渲染的 JSON，包含：

```text
name / english / reason / recipe / sequence / image / source / emotion
```

## 接入真实机械臂

1. 在 `robot_points.json` 里把每个点位的 `approach` 和 `pick` 改成实测 6 关节角。
2. 确认 `sdk_scripts_path` 指向当前机器上真实存在的 `Panthera-HT_SDK/panthera_python/scripts`。
3. 确认 `robot_config_path` 使用正确的 `Follower.yaml` 或 `Leader.yaml`。
4. 联调阶段建议先把 `dry_run` 设为 `true`，这样前端、推荐逻辑和动作编排都能先跑通。
5. 等 SDK 导入和真实点位都确认无误后，再改回 `dry_run: false`。

## 指令流程

前端点击开始调制后，会向本地服务发送：

```http
POST /api/run
```

服务按酒品原料编号顺序执行：

```text
home -> 点位approach -> 点位pick -> 夹取 -> mix -> 等待 -> 返回pick -> 松开 -> home
```

`dry_run: true` 时只模拟状态和日志，不调用 Panthera SDK。
