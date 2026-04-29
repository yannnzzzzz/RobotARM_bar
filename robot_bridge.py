#!/usr/bin/env python3
"""Local HTTP bridge between the bar UI and Panthera-HT SDK."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import argparse
import json
import sys
import threading
import time
import traceback


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "robot_points.json"
AI_ROOT = ROOT.parent / "ai"

if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

DRINKS = {
    1: {"name": "椰林飘香", "sequence": ["01", "02", "03"]},
    2: {"name": "蓝色椰风", "sequence": ["04", "03", "02"]},
    3: {"name": "桂花橙香", "sequence": ["05", "06"]},
    4: {"name": "柠檬暴击啤", "sequence": ["07", "06"]},
}

STATUS = {
    "connected": False,
    "dry_run": False,
    "busy": False,
    "state": "idle",
    "message": "服务已启动，等待指令",
    "activeDrink": None,
    "currentPoint": None,
    "currentSegment": None,
    "progress": 0,
    "logs": [],
}
STATUS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_config(config):
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2, ensure_ascii=False)


def update_status(**changes):
    with STATUS_LOCK:
        STATUS.update(changes)
        message = changes.get("message")
        if message:
            STATUS["logs"] = ([message] + STATUS["logs"])[:80]


def status_snapshot():
    with STATUS_LOCK:
        return dict(STATUS)


class PantheraRunner:
    def __init__(self, config):
        self.config = config
        self.robot = None
        self.TrajectoryRecorder = None
        self.dry_run = bool(config.get("dry_run", True))
        update_status(dry_run=self.dry_run, connected=self.dry_run)

    def connect(self):
        if self.dry_run:
            update_status(connected=True, message="当前为 dry_run 模式，仅模拟机械臂动作")
            return

        if self.robot is not None:
            update_status(connected=True, message="机械臂已连接")
            return

        sdk_scripts = self.config.get("sdk_scripts_path")
        if sdk_scripts and sdk_scripts not in sys.path:
            sys.path.insert(0, sdk_scripts)

        from Panthera_lib import Panthera, TrajectoryRecorder

        robot_config = self.config.get("robot_config_path") or None
        self.robot = Panthera(robot_config)
        self.TrajectoryRecorder = TrajectoryRecorder
        self.robot.send_get_motor_state_cmd()
        self.robot.motor_send_cmd()
        update_status(connected=True, message="机械臂 SDK 初始化完成")

    def record_home(self):
        self.connect()
        if self.dry_run:
            raise RuntimeError("dry_run 模式无法记录真实点位")

        joint_angles = self.robot.get_current_pos().tolist()
        self.config.setdefault("positions", {})["home"] = joint_angles
        save_config(self.config)
        update_status(message=f"home 已记录: {joint_angles}")

    def run_drink(self, drink_id, sequence=None, drink_name=None):
        drink_id = int(drink_id)
        if drink_id not in DRINKS and not sequence:
            raise ValueError(f"未知酒品编号: {drink_id}")

        drink = DRINKS.get(drink_id, {"name": drink_name or "MomoTender"})
        sequence = sequence or drink["sequence"]
        resolved_name = drink_name or drink["name"]
        self._validate_sequence(sequence)

        if not RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("机械臂正在执行上一条任务")

        try:
            update_status(
                busy=True,
                state="running",
                activeDrink={"id": drink_id, "name": resolved_name, "sequence": sequence},
                currentPoint=None,
                currentSegment=None,
                progress=0,
                message=f"开始执行 {drink_id}号 {resolved_name}",
            )
            self.connect()

            if self.config.get("move_home_before_run", True):
                self._move_home()

            total = len(sequence)
            for index, point_id in enumerate(sequence, start=1):
                self._execute_point(point_id, index, total)

            if self.config.get("move_home_after_run", True):
                self._move_home()

            update_status(
                busy=False,
                state="done",
                currentPoint=None,
                currentSegment=None,
                progress=100,
                message=f"{resolved_name} 调制完成，机械臂已复位",
            )
        except Exception as exc:
            update_status(
                busy=False,
                state="error",
                message=f"执行失败: {exc}",
            )
            traceback.print_exc()
        finally:
            RUN_LOCK.release()

    def reset(self):
        if not RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("机械臂正在执行任务，无法复位")

        try:
            update_status(state="resetting", busy=True, message="正在复位机械臂")
            self.connect()
            self._gripper_close()
            self._move_home()
            update_status(
                state="idle",
                busy=False,
                currentPoint=None,
                currentSegment=None,
                progress=0,
                message="机械臂已复位",
            )
        except Exception as exc:
            update_status(state="error", busy=False, message=f"复位失败: {exc}")
            raise
        finally:
            RUN_LOCK.release()

    def _validate_sequence(self, sequence):
        points = self.config.get("points", {})
        for point_id in sequence:
            point = points.get(str(point_id))
            if not point:
                raise ValueError(f"点位 {point_id} 未配置")

            if "trajectories" in point:
                self._validate_trajectories(str(point_id), point["trajectories"])
            else:
                self._validate_joint_array(point.get("approach"), f"{point_id}.approach")
                self._validate_joint_array(point.get("pick"), f"{point_id}.pick")

        self._validate_joint_array(self.config.get("positions", {}).get("home"), "positions.home")
        if "mix" in self.config.get("positions", {}):
            self._validate_joint_array(self.config["positions"]["mix"], "positions.mix")

    def _validate_trajectories(self, point_id, trajectories):
        for segment in self._trajectory_segment_order():
            filepath = trajectories.get(segment)
            if not filepath:
                raise ValueError(f"点位 {point_id} 缺少轨迹片段: {segment}")
            if not self.dry_run and not self._resolve_path(filepath).is_file():
                raise ValueError(f"轨迹文件不存在: {filepath}")

    @staticmethod
    def _validate_joint_array(value, label):
        if not isinstance(value, list) or len(value) != 6:
            raise ValueError(f"{label} 必须是 6 个关节角数组")

    def _execute_point(self, point_id, index, total):
        point_id = str(point_id)
        point = self.config["points"][point_id]

        if "trajectories" not in point:
            self._execute_point_by_positions(point_id, point, index, total)
            return

        segment_order = self._trajectory_segment_order()
        for segment_index, segment in enumerate(segment_order, start=1):
            progress = self._segment_progress(index, total, segment_index, len(segment_order))
            update_status(
                state=self._segment_state(segment),
                currentPoint=point_id,
                currentSegment=segment,
                progress=progress,
                message=f"播放点位 {point_id} {point['name']} 轨迹: {segment}",
            )
            self._play_trajectory(point_id, point, segment)
            self._sleep_after_trajectory_segment(point_id, point, segment)

        update_status(
            currentSegment=None,
            progress=int(index / total * 100),
            message=f"点位 {point_id} 完成",
        )

    def _execute_point_by_positions(self, point_id, point, index, total):
        progress_base = int((index - 1) / total * 100)
        update_status(
            state="picking",
            currentPoint=point_id,
            currentSegment=None,
            progress=progress_base,
            message=f"前往点位 {point_id} {point['name']}",
        )
        self._gripper_open()
        self._move(point["approach"], f"{point_id} approach")
        self._move(point["pick"], f"{point_id} pick")

        update_status(message=f"夹取 {point_id} {point['name']}")
        self._gripper_close()
        self._sleep("grip_wait_seconds")

        self._move(point["approach"], f"{point_id} lift")
        update_status(
            state="mixing",
            progress=min(progress_base + int(45 / total), 95),
            message=f"{point['name']} 移动至调配位",
        )
        self._move(self.config["positions"]["mix"], "mix")

        update_status(message=f"{point['name']} 回放原点位 {point_id}")
        self._move(point["approach"], f"{point_id} return approach")
        self._move(point["pick"], f"{point_id} return pick")
        self._gripper_open()
        self._sleep("return_wait_seconds")
        self._move(point["approach"], f"{point_id} clear")
        update_status(progress=int(index / total * 100), message=f"点位 {point_id} 完成")

    def _trajectory_segment_order(self):
        playback = self.config.get("trajectory_playback", {})
        return playback.get("segment_order", ["to_mix", "return"])

    @staticmethod
    def _segment_state(segment):
        if segment == "pick":
            return "picking"
        if segment == "to_mix":
            return "mixing"
        if segment == "return":
            return "returning"
        return "running"

    @staticmethod
    def _segment_progress(point_index, point_total, segment_index, segment_total):
        completed_points = point_index - 1
        completed_segments = segment_index - 1
        progress = (completed_points + completed_segments / segment_total) / point_total * 100
        return min(int(progress), 99)

    def _sleep_after_trajectory_segment(self, point_id, point, segment):
        playback = self.config.get("trajectory_playback", {})
        delays = playback.get("delay_after_segments_seconds", {})
        seconds = float(delays.get(segment, 0.0))
        if seconds <= 0:
            return

        update_status(message=f"点位 {point_id} {point['name']} 轨迹 {segment} 完成，等待 {seconds:g} 秒")
        time.sleep(seconds if not self.dry_run else min(seconds, 0.3))

    def _play_trajectory(self, point_id, point, segment):
        filepath = point["trajectories"][segment]
        resolved_path = self._resolve_path(filepath)

        if self.dry_run:
            time.sleep(0.35)
            return

        if self.TrajectoryRecorder is None:
            raise RuntimeError("TrajectoryRecorder 未初始化")

        playback = self.config.get("trajectory_playback", {})
        self.TrajectoryRecorder.play(
            robot=self.robot,
            filepath=str(resolved_path),
            kp=playback.get("kp", [30.0, 40.0, 55.0, 15.0, 7.0, 5.0]),
            kd=playback.get("kd", [3.0, 4.0, 5.5, 1.5, 0.7, 0.5]),
            fc=playback.get("fc", [0.15, 0.12, 0.12, 0.12, 0.04, 0.04]),
            fv=playback.get("fv", [0.05, 0.05, 0.05, 0.03, 0.02, 0.02]),
            vel_threshold=float(playback.get("vel_threshold", 0.02)),
            tau_limit=playback.get("tau_limit", [15.0, 30.0, 30.0, 15.0, 5.0, 5.0]),
            gripper_kp=float(playback.get("gripper_kp", 5.0)),
            gripper_kd=float(playback.get("gripper_kd", 0.5)),
        )

    def _move_home(self):
        update_status(state="homing", currentPoint=None, currentSegment=None, message="机械臂复位")
        self._move(self.config["positions"]["home"], "home")

    def _move(self, joints, label):
        motion = self.config.get("motion", {})
        if self.dry_run:
            time.sleep(0.25)
            return
        self.robot.moveJ(
            joints,
            duration=float(motion.get("duration", 2.5)),
            max_tqu=motion.get("max_torque"),
            iswait=True,
            timeout=max(float(motion.get("duration", 2.5)) + 8.0, 15.0),
        )

    def _gripper_open(self):
        motion = self.config.get("motion", {})
        if self.dry_run:
            time.sleep(0.1)
            return
        self.robot.gripper_open(pos=float(motion.get("gripper_open_pos", 1.6)))

    def _gripper_close(self):
        motion = self.config.get("motion", {})
        if self.dry_run:
            time.sleep(0.1)
            return
        self.robot.gripper_close(pos=float(motion.get("gripper_close_pos", 0.0)))

    def _sleep(self, key):
        seconds = float(self.config.get("motion", {}).get(key, 1.0))
        time.sleep(seconds if not self.dry_run else min(seconds, 0.3))

    @staticmethod
    def _resolve_path(filepath):
        path = Path(filepath)
        if path.is_absolute():
            return path
        return ROOT / path


RUNNER = PantheraRunner(load_config())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/api/status":
            self._send_json(status_snapshot())
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/run":
                drink_id = body.get("cocktailId")
                sequence = body.get("sequence")
                drink_name = body.get("cocktailName")
                thread = threading.Thread(
                    target=RUNNER.run_drink,
                    args=(drink_id, sequence, drink_name),
                    daemon=True,
                )
                thread.start()
                self._send_json({"ok": True, "message": "任务已接收"})
                return
            import sys
            sys.path.append("/media/yan/D/Moce/Code/ai/ai")
            if path == "/api/momotender/recommend":
                from momotender_service import recommend_for_web

                self._send_json(recommend_for_web())
                return
            if path == "/api/connect":
                RUNNER.connect()
                self._send_json({"ok": True, "status": status_snapshot()})
                return
            if path == "/api/record_home":
                RUNNER.record_home()
                self._send_json({"ok": True, "message": "home 点位已记录"})
                return
            if path == "/api/reset":
                RUNNER.reset()
                self._send_json({"ok": True})
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Robot bar Panthera-HT bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Robot bridge running at http://{args.host}:{args.port}")
    print(f"Open http://{args.host}:{args.port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
