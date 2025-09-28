#!/usr/bin/env python3
import asyncio
import json
import threading
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy

from std_msgs.msg import String as RosString
from geometry_msgs.msg import Point as GeoPoint
from ros2_lam_interfaces.srv import SetExclusionArea
from std_srvs.srv import Trigger


# ---------------- ROS Bridge ----------------
class RosBridge(Node):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__("lam_web_bridge")
        self.loop = loop

        # Service clients
        self.cli_set = self.create_client(SetExclusionArea, "set_exclusion_area")
        self.cli_add = self.create_client(SetExclusionArea, "add_exclusion_area")
        self.cli_clear = self.create_client(Trigger, "clear_exclusion_areas")

        # Subscribers
        qos_lat = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.sub_areas = self.create_subscription(RosString, "/lam/areas", self._on_areas, qos_lat)
        self.sub_tracks = self.create_subscription(RosString, "/lam/tracks", self._on_tracks, 10)

        # State
        self.latest_areas = {"areas": []}
        self.ws_clients: List[WebSocket] = []

    # Callbacks
    def _on_areas(self, msg: RosString):
        try:
            self.latest_areas = json.loads(msg.data)
        except Exception:
            self.get_logger().warn("Invalid /lam/areas JSON")

    def _on_tracks(self, msg: RosString):
        # broadcast to all websockets (from ROS thread -> schedule on asyncio loop)
        asyncio.run_coroutine_threadsafe(self._broadcast(msg.data), self.loop)

    async def _broadcast(self, payload: str):
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for d in dead:
            if d in self.ws_clients:
                self.ws_clients.remove(d)

    # Service helpers
    def _wait_for_service(self, client, timeout=3.0):
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError("Service not available")

    def _call_set(self, coords):
        self._wait_for_service(self.cli_set)
        req = SetExclusionArea.Request()
        req.point_list = [GeoPoint(x=float(x), y=float(y), z=0.0) for (x, y) in coords]
        fut = self.cli_set.call_async(req)
        rclpy.spin_until_future_complete(self, fut, timeout_sec=5.0)
        res = fut.result()
        if not res or not res.is_ok:
            raise RuntimeError(res.error_msg if res else "No response")
        return res

    def _call_add(self, coords):
        self._wait_for_service(self.cli_add)
        req = SetExclusionArea.Request()
        req.point_list = [GeoPoint(x=float(x), y=float(y), z=0.0) for (x, y) in coords]
        fut = self.cli_add.call_async(req)
        rclpy.spin_until_future_complete(self, fut, timeout_sec=5.0)
        res = fut.result()
        if not res or not res.is_ok:
            raise RuntimeError(res.error_msg if res else "No response")
        return res

    def _call_clear(self):
        self._wait_for_service(self.cli_clear)
        req = Trigger.Request()
        fut = self.cli_clear.call_async(req)
        rclpy.spin_until_future_complete(self, fut, timeout_sec=5.0)
        res = fut.result()
        if not res or not res.success:
            raise RuntimeError(res.message if res else "No response")
        return res


# ---------------- FastAPI App ----------------
app = FastAPI()

# init ROS in background
loop = asyncio.get_event_loop()
rclpy.init()
bridge = RosBridge(loop=loop)

def ros_spin():
    rclpy.spin(bridge)

ros_thread = threading.Thread(target=ros_spin, daemon=True)
ros_thread.start()


# REST: Areas
@app.get("/areas")
def get_areas():
    return JSONResponse(content=bridge.latest_areas)

@app.post("/areas")
def add_area(payload: dict):
    coords = payload.get("coords", [])
    if not coords or len(coords) < 3:
        raise HTTPException(status_code=400, detail="coords must be a list of at least 3 [x,y] points")
    try:
        bridge._call_add(coords)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/areas")
def set_area(payload: dict):
    coords = payload.get("coords", [])
    if not coords or len(coords) < 3:
        raise HTTPException(status_code=400, detail="coords must be a list of at least 3 [x,y] points")
    try:
        bridge._call_set(coords)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/areas")
def clear_areas():
    try:
        bridge._call_clear()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# WebSocket: Tracks stream
@app.websocket("/ws/tracks")
async def ws_tracks(ws: WebSocket):
    await ws.accept()
    bridge.ws_clients.append(ws)
    try:
        while True:
            # keep the socket open; ignore client messages
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in bridge.ws_clients:
            bridge.ws_clients.remove(ws)

# Serve static files (index.html) — mount AFTER routes to avoid conflicts
app.mount("/", StaticFiles(directory="static", html=True), name="static")
