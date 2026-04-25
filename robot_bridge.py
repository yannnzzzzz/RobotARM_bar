#!/usr/bin/env python3
"""Local HTTP bridge between the self-service bar UI and Panthera-HT SDK."""

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
    "progress": 0,
    "logs": [],
}
STATUS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


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

        from Panthera_lib import Panthera

        robot_config = self.config.get("robot_config_path") or None
        self.robot = Panthera(robot_config)
        self.robot.send_get_motor_state_cmd()
        self.robot.motor_send_cmd()
        update_status(connected=True, message="机械臂 SDK 初始化完成")

    def run_drink(self, drink_id, sequence=None):
        drink_id = int(drink_id)
        if drink_id not in DRINKS:
            raise ValueError(f"未知酒品编号: {drink_id}")

        drink = DRINKS[drink_id]
        sequence = sequence or drink["sequence"]
        self._validate_sequence(sequence)

        if not RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("机械臂正在执行上一条任务")

        try:
            update_status(
                busy=True,
                state="running",
                activeDrink={"id": drink_id, "name": drink["name"], "sequence": sequence},
                currentPoint=None,
                progress=0,
                message=f"开始执行 {drink_id}号 {drink['name']}",
            )
            self.connect()
            self._move_home()

            total = len(sequence)
            for index, point_id in enumerate(sequence, start=1):
                self._execute_point(point_id, index, total)

            self._move_home()
            update_status(
                busy=False,
                state="done",
                currentPoint=None,
                progress=100,
                message=f"{drink['name']} 调制完成，机械臂已复位",
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

    def _validate_sequence(self, sequence):
        points = self.config["points"]
        for point_id in sequence:
            point = points.get(str(point_id))
            if not point:
                raise ValueError(f"点位 {point_id} 未配置")
            self._validate_joint_array(point["approach"], f"{point_id}.approach")
            self._validate_joint_array(point["pick"], f"{point_id}.pick")
        self._validate_joint_array(self.config["positions"]["home"], "positions.home")
        self._validate_joint_array(self.config["positions"]["mix"], "positions.mix")

    @staticmethod
    def _validate_joint_array(value, label):
        if not isinstance(value, list) or len(value) != 6:
            raise ValueError(f"{label} 必须是 6 个关节角数组")

    def _execute_point(self, point_id, index, total):
        point_id = str(point_id)
        point = self.config["points"][point_id]
        progress_base = int(((index - 1) / total) * 100)

        update_status(
            state="picking",
            currentPoint=point_id,
            progress=progress_base,
            message=f"前往点位 {point_id} {point['name']}",
        )
        self._gripper_open()
        self._move(point["approach"], f"{point_id} approach")
        #self._move(point["pick"], f"{point_id} pick")

        update_status(message=f"夹取 {point_id} {point['name']}")
        self._gripper_close()
        self._sleep("grip_wait_seconds")

        #self._move(point["approach"], f"{point_id} lift")
        update_status(
            state="mixing",
            progress=min(progress_base + int(45 / total), 95),
            message=f"{point['name']} 移动至调配位",
        )
        #self._move(self.config["positions"]["mix"], "mix")
        self._sleep("pour_wait_seconds")

        update_status(message=f"{point['name']} 回放原点位 {point_id}")
        #self._move(point["approach"], f"{point_id} return approach")
        #self._move(point["pick"], f"{point_id} return pick")
        self._gripper_open()
        self._sleep("return_wait_seconds")
        #self._move(point["approach"], f"{point_id} clear")
        update_status(progress=int((index / total) * 100), message=f"点位 {point_id} 完成")

    def _move_home(self):
        update_status(state="homing", currentPoint=None, message="机械臂复位")
        self._move(self.config["positions"]["home"], "home")

    def _move(self, joints, label):
        motion = self.config["motion"]
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
        motion = self.config["motion"]
        if self.dry_run:
            time.sleep(0.1)
            return
        self.robot.gripper_open(pos=float(motion.get("gripper_open_pos", 1.6)))

    def _gripper_close(self):
        motion = self.config["motion"]
        if self.dry_run:
            time.sleep(0.1)
            return
        self.robot.gripper_close(pos=float(motion.get("gripper_close_pos", 0.0)))

    def _sleep(self, key):
        seconds = float(self.config["motion"].get(key, 1.0))
        time.sleep(seconds if not self.dry_run else min(seconds, 0.3))


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
                thread = threading.Thread(
                    target=RUNNER.run_drink,
                    args=(drink_id, sequence),
                    daemon=True,
                )
                thread.start()
                self._send_json({"ok": True, "message": "任务已接收"})
                return
            if path == "/api/connect":
                RUNNER.connect()
                self._send_json({"ok": True, "status": status_snapshot()})
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Robot bridge running at http://{args.host}:{args.port}")
    print(f"Open http://{args.host}:{args.port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
