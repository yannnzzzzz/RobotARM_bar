# 智能自助调酒系统机械臂桥接

## 运行

```powershell
cd D:\Moce\Code\robot_bar
python robot_bridge.py
```

打开：

```text
http://127.0.0.1:8765/index.html
```

## 接入真实机械臂

1. 在 `robot_points.json` 里把每个点位的 `approach` 和 `pick` 改成实测 6 关节角。
2. 确认 `sdk_scripts_path` 指向 `Panthera-HT_SDK/panthera_python/scripts`。
3. 确认 `robot_config_path` 使用正确的 `Follower.yaml` 或 `Leader.yaml`。
4. 标定完成后将 `dry_run` 改为 `false`。
5. 重新启动 `python robot_bridge.py`。

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
