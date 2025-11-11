#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio, json, math, threading, atexit, os, time
from typing import List, Dict, Any, Optional

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
from rcl_interfaces.srv import GetParameters

from led_manager import LedManager


# ---------------- Utils ----------------
def jdumps(obj: Any) -> str:
    # JSON compatto per ridurre banda/CPU
    return json.dumps(obj, separators=(",", ":"))


# ---------------- ROS Bridge ----------------
class RosBridge(Node):
    """Bridge ROS2 ⇄ FastAPI/WS.
    - Cache ultimi messaggi /lam/areas, /lam/tracks, /lam/uwb_tags, /lam/assocs
    - Push WS coalesced ~16 FPS (riduce carico)
    - Poll 1 Hz dei parametri dal nodo LAM per refs + soglie associazione (non bloccante)
    - Calcolo associazioni TAG↔cluster lato server come fallback (se non ricevo /lam/assocs)
    """
    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None,
                 lam_node_name: str = "lidar_area_monitor_node"):
        super().__init__("lam_web_bridge")
        self.loop = loop  # verrà impostato al primo WS se non passato
        self.lam_node_name = lam_node_name

        # ----- Service clients (aree)
        self.cli_set   = self.create_client(SetExclusionArea, "set_exclusion_area")
        self.cli_add   = self.create_client(SetExclusionArea, "add_exclusion_area")
        self.cli_clear = self.create_client(Trigger,          "clear_exclusion_areas")

        # ----- Parameter client verso nodo principale
        self.cli_params = self.create_client(GetParameters, f"/{self.lam_node_name}/get_parameters")

        # ----- Subscribers
        qos_lat = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.sub_areas  = self.create_subscription(RosString, "/lam/areas",  self._on_areas,  qos_lat)
        self.sub_tracks = self.create_subscription(RosString, "/lam/tracks", self._on_tracks, 10)
        self.sub_uwb    = self.create_subscription(RosString, "/lam/uwb_tags", self._on_uwb, 10)
        self.sub_assocs = self.create_subscription(RosString, "/lam/assocs", self._on_assocs, 10)

        # ----- Stato/cache
        self.latest_areas: Dict[str, Any]   = {"areas": []}
        self.latest_tracks: Dict[str, Any]  = {"stamp": 0.0, "tracks": []}
        self.latest_tags: Dict[str, Any]    = {"stamp": 0.0, "tags": []}
        self.latest_assocs: Dict[str, Any]  = {"stamp": 0.0, "assocs": []}

        self.refs: Dict[str, Any] = {
            "lidar":   {"axis_len": 0.35},
            "antenna": {"x": 0.0, "y": 0.0, "yaw_rad": 0.0, "axis_len": 0.35},
            "assoc":   {"radius": 0.25, "bbox_margin": 0.02, "mode": "both"}
        }
        self._last_refs_sent = None

        # WebSocket clients
        self.ws_clients: List[WebSocket] = []

        # ----- LED manager
        self.led_manager = LedManager(self)
        atexit.register(self.led_manager.shutdown)

        # ----- Timers
        self.ws_fps = float(os.environ.get("WS_FPS", "16"))
        self._ws_period = max(0.02, 1.0 / self.ws_fps)
        self._ws_timer = self.create_timer(self._ws_period, self._pump_ws)  # ~16 FPS
        self._refs_timer = self.create_timer(1.0, self._poll_refs)          # 1 Hz

        # Staleness tracking
        self._last_tracks_mono = time.monotonic()
        self._last_tags_mono = time.monotonic()

    # ----- Topic callbacks -----
    def _on_areas(self, msg: RosString):
        try:
            self.latest_areas = json.loads(msg.data)
        except Exception:
            self.get_logger().warn("Invalid /lam/areas JSON")

    def _on_tracks(self, msg: RosString):
        try:
            self.latest_tracks = json.loads(msg.data)
            self._last_tracks_mono = time.monotonic()
        except Exception:
            pass  # ignora frame malformati

    def _on_uwb(self, msg: RosString):
        """Normalizza i TAG in METRI: usa x_m/y_m/d_m se presenti, altrimenti converte da cm."""
        try:
            raw = json.loads(msg.data)
            units = str(raw.get("units", "m")).lower()
            stamp = float(raw.get("stamp", time.time()))
            norm = []
            for t in raw.get("tags", []):
                tid = str(t.get("id", "")).strip()
                if not tid:
                    continue
                if all(k in t for k in ("x_m", "y_m", "d_m")):
                    x = float(t["x_m"]); y = float(t["y_m"]); d = float(t["d_m"])
                else:
                    x = float(t.get("x", 0.0)); y = float(t.get("y", 0.0)); d = float(t.get("d", 0.0))
                    if units == "cm":
                        x *= 0.01; y *= 0.01; d *= 0.01
                norm.append({"id": tid, "x": x, "y": y, "d": d})
            self.latest_tags = {"stamp": stamp, "tags": norm}
            self._last_tags_mono = time.monotonic()
        except Exception:
            # ignora frame malformati
            pass

    def _on_assocs(self, msg: RosString):
        """Inoltra /lam/assocs e aggiorna cache per snapshot WS."""
        try:
            data = json.loads(msg.data)
            self.latest_assocs = data
        except Exception:
            return
        payload = jdumps({"type": "assocs", "data": self.latest_assocs})
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self.loop)

    # ----- Poll refs/params (1 Hz, non bloccante) -----
    def _poll_refs(self):
        names = [
            "antenna_dx", "antenna_dy", "antenna_yaw_deg",
            "antenna_axis_len", "lidar_axis_len",
            "tag_assoc_dist", "verify_bbox_margin", "verify_mode"
        ]
        if not self.cli_params.wait_for_service(timeout_sec=0.01):
            return
        try:
            req = GetParameters.Request(names=names)
            fut = self.cli_params.call_async(req)

            def _done(fut_):
                try:
                    res = fut_.result()
                    if not res:
                        return
                    vals: Dict[str, Any] = {}
                    for name, val in zip(names, res.values):
                        # rcl_interfaces/msg/ParameterValue.type:
                        # 2=int, 3=double, 4=string
                        if val.type == 3:
                            v: Any = val.double_value
                        elif val.type == 2:
                            v = float(val.integer_value)
                        elif val.type == 4:
                            v = val.string_value
                        else:
                            continue
                        vals[name] = v
                    verify_mode = str(vals.get("verify_mode", "both")).lower()
                    new_refs = {
                        "lidar":   {"axis_len": float(vals.get("lidar_axis_len", 0.35))},
                        "antenna": {
                            "x": float(vals.get("antenna_dx", 0.0)),
                            "y": float(vals.get("antenna_dy", 0.0)),
                            "yaw_rad": math.radians(float(vals.get("antenna_yaw_deg", 0.0))),
                            "axis_len": float(vals.get("antenna_axis_len", 0.35))
                        },
                        "assoc": {
                            "radius": float(vals.get("tag_assoc_dist", 0.25)),
                            "bbox_margin": float(vals.get("verify_bbox_margin", 0.02)),
                            "mode": verify_mode,
                        }
                    }
                    self.refs = new_refs
                except Exception as e:
                    try:
                        self.get_logger().debug(f"refs poll err: {e}")
                    except Exception:
                        pass

            fut.add_done_callback(_done)
        except Exception as e:
            try:
                self.get_logger().debug(f"refs poll err: {e}")
            except Exception:
                pass

    # ----- Associazione TAG↔cluster (fallback locale) -----
    def _compute_assocs(self) -> Dict[str, Any]:
        """Calcolo locale (usato solo come snapshot se non è ancora arrivato /lam/assocs)."""
        tracks = self.latest_tracks.get("tracks", []) or []
        tags = self.latest_tags.get("tags", []) or []
        assoc = self.refs.get("assoc", {}) if isinstance(self.refs, dict) else {}
        radius = float(assoc.get("radius", 0.25))
        margin = float(assoc.get("bbox_margin", 0.02))
        mode = str(assoc.get("mode", "both")).lower()

        assocs = []
        for t in tracks:
            tx = float(t.get("x", 0.0)); ty = float(t.get("y", 0.0))
            best = None; best_d = 1e9; best_id = None
            for g in tags:
                d = math.hypot(tx - float(g.get("x", 0.0)), ty - float(g.get("y", 0.0)))
                if d < best_d:
                    best_d, best = d, g
                    best_id = str(g.get("id", "")) if "id" in g else None
            if best is None:
                continue
            by_radius = (best_d <= radius)
            by_bbox = False
            bb = t.get("bbox", None)
            if bb is not None and len(bb) == 4:
                minx, miny, maxx, maxy = [float(v) for v in bb]
                bx = float(best.get("x", 0.0)); by = float(best.get("y", 0.0))
                by_bbox = (minx - margin <= bx <= maxx + margin) and (miny - margin <= by <= maxy + margin)

            if mode == "centroid_radius":
                verified = by_radius
            elif mode == "bbox":
                verified = by_bbox
            else:  # both
                verified = by_radius or by_bbox

            assocs.append({
                "track_id": int(t.get("id", -1)),
                "tag_id": best_id,
                "distance": best_d,            # chiave allineata al nodo
                "by_radius": bool(by_radius),
                "by_bbox": bool(by_bbox),
                "verified": bool(verified),
            })
        return {"stamp": self.latest_tracks.get("stamp", 0.0), "assocs": assocs}

    # ----- WS broadcast throttled -----
    def _pump_ws(self):
        if not self.ws_clients or not self.loop:
            return

        # refs: invia solo se cambiate (1 Hz circa)
        if self._last_refs_sent != self.refs:
            self._last_refs_sent = json.loads(jdumps(self.refs))  # deep-copy cheap
            payload = jdumps({"type": "refs", "data": self.refs})
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self.loop)

        # tracks & tags (coalesced)
        asyncio.run_coroutine_threadsafe(
            self._broadcast(jdumps({"type": "tracks", "data": self.latest_tracks})),
            self.loop
        )
        asyncio.run_coroutine_threadsafe(
            self._broadcast(jdumps({"type": "uwb", "data": self.latest_tags})),
            self.loop
        )
        # NOTA: 'assocs' arriva push da /lam/assocs in _on_assocs; niente doppioni qui.

    # ----- WS broadcast helper -----
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

    # ----- Service helpers (sincroni; eseguiti in threadpool dagli endpoint) -----
    def _wait_for_service(self, client, timeout=3.0):
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError("Service not available")

    @staticmethod
    def _wait_future(fut, timeout: float = 5.0):
        deadline = time.time() + timeout
        while not fut.done() and time.time() < deadline and rclpy.ok():
            time.sleep(0.01)
        return fut.result() if fut.done() else None

    def _call_set(self, coords):
        self._wait_for_service(self.cli_set)
        req = SetExclusionArea.Request()
        req.point_list = [GeoPoint(x=float(x), y=float(y), z=0.0) for (x, y) in coords]
        fut = self.cli_set.call_async(req)
        res = self._wait_future(fut, timeout=5.0)
        if not res or not res.is_ok:
            raise RuntimeError(res.error_msg if res else "No response")
        return {"ok": True}

    def _call_add(self, coords):
        self._wait_for_service(self.cli_add)
        req = SetExclusionArea.Request()
        req.point_list = [GeoPoint(x=float(x), y=float(y), z=0.0) for (x, y) in coords]
        fut = self.cli_add.call_async(req)
        res = self._wait_future(fut, timeout=5.0)
        if not res or not res.is_ok:
            raise RuntimeError(res.error_msg if res else "No response")
        return {"ok": True}

    def _call_clear(self):
        self._wait_for_service(self.cli_clear)
        req = Trigger.Request()
        fut = self.cli_clear.call_async(req)
        res = self._wait_future(fut, timeout=5.0)
        if not res or not res.success:
            raise RuntimeError(res.message if res else "No response")
        return {"ok": True}


# ---------------- FastAPI App ----------------
app = FastAPI()

# init ROS in background
rclpy.init()
bridge = RosBridge(loop=None)  # il loop vero lo settiamo al primo WS

def ros_spin():
    rclpy.spin(bridge)

ros_thread = threading.Thread(target=ros_spin, daemon=True)
ros_thread.start()

# Shutdown ROS pulito all’uscita
def _shutdown_ros():
    try:
        rclpy.shutdown()
    except Exception:
        pass
atexit.register(_shutdown_ros)

# --------- REST: Areas (async + threadpool -> non blocca event loop) ---------
@app.get("/areas")
async def get_areas():
    return JSONResponse(content=bridge.latest_areas)

@app.post("/areas")
async def add_area(payload: dict):
    coords = payload.get("coords", [])
    if not coords or len(coords) < 3:
        raise HTTPException(status_code=400, detail="coords must be a list of at least 3 [x,y] points")
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, bridge._call_add, coords)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/areas")
async def set_area(payload: dict):
    coords = payload.get("coords", [])
    if not coords or len(coords) < 3:
        raise HTTPException(status_code=400, detail="coords must be a list of at least 3 [x,y] points")
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, bridge._call_set, coords)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/areas")
async def clear_areas():
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, bridge._call_clear)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --------- WebSocket unificato ---------
@app.websocket("/ws")
async def ws_unified(ws: WebSocket):
    await ws.accept()

    # Assicura che il bridge conosca il loop reale dell'app
    if bridge.loop is None:
        bridge.loop = asyncio.get_running_loop()

    bridge.ws_clients.append(ws)
    # snapshot immediato (refs + ultimo stato)
    try:
        await ws.send_text(jdumps({"type": "refs", "data": bridge.refs}))
        await ws.send_text(jdumps({"type": "tracks", "data": bridge.latest_tracks}))
        await ws.send_text(jdumps({"type": "uwb", "data": bridge.latest_tags}))

        # snapshot assocs: preferisci quelle del nodo; se non disponibili, calcolo locale
        assocs_snapshot = bridge.latest_assocs if bridge.latest_assocs.get("assocs") else bridge._compute_assocs()
        await ws.send_text(jdumps({"type": "assocs", "data": assocs_snapshot}))
    except Exception:
        pass
    try:
        while True:
            # mantieni viva la connessione; ignoriamo dati dal client
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in bridge.ws_clients:
            bridge.ws_clients.remove(ws)

# Backward compatibility: vecchio endpoint solo-tracks (opzionale)
@app.websocket("/ws/tracks")
async def ws_tracks(ws: WebSocket):
    await ws_unified(ws)

# Static (index.html nella cartella "static")
# NB: montiamo alla fine, cattura solo path non gestiti sopra
app.mount("/", StaticFiles(directory="static", html=True), name="static")
