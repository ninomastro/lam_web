# led_manager.py
import json, math, time
from enum import Enum
from typing import Tuple, List

from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from std_msgs.msg import String as RosString
from sensor_msgs.msg import LaserScan

try:
    import board
    import neopixel_spi as neopixel  # adafruit-circuitpython-neopixel-spi
    _HAVE_HW = True
except Exception as e:
    _HAVE_HW = False
    _HW_ERR = e


class LedState(Enum):
    CONFIG = 0        # nessuno scan recente → attesa/config
    FREE_PENDING = 1  # appena liberata → giallo transitorio
    FREE = 2          # libera stabile → verde respiro
    STATIC = 3        # ID presenti ma lenti
    MOVING = 4        # almeno un ID veloce


class LedManager:
    """Gestisce i LED NeoPixel via SPI, usando il Node esistente per sub/timer."""
    def __init__(self, node: Node):
        self.n = node
        self.enabled = _HAVE_HW
        if not self.enabled:
            self.n.get_logger().warn(f"LED disabilitati (HW non disponibile): {_HW_ERR}")
            return

        # --- Parametri ROS (prefisso led_) ---
        self.num_pixels = int(self._declare_get("led_num_pixels", 24))
        self.brightness = float(self._declare_get("led_brightness", 0.5))
        self.v_move = float(self._declare_get("led_moving_speed_threshold", 0.15))   # m/s
        self.stale_scan_sec = float(self._declare_get("led_stale_scan_sec", 2.0))    # → CONFIG
        self.free_transition_sec = float(self._declare_get("led_free_transition_sec", 2.0))

        self.color_config = tuple(self._declare_get("led_color_config", [255, 100, 0]))        # arancione
        self.color_free = tuple(self._declare_get("led_color_free", [0, 255, 50]))             # verde
        self.color_free_pending = tuple(self._declare_get("led_color_free_pending", [255, 200, 0])) # giallo
        self.color_static = tuple(self._declare_get("led_color_static", [255, 140, 0]))        # arancione vivo
        self.color_moving = tuple(self._declare_get("led_color_moving", [255, 0, 0]))          # rosso

        self.period_config = float(self._declare_get("led_period_config", 0.6))
        self.period_static = float(self._declare_get("led_period_static", 0.3))
        self.period_moving = float(self._declare_get("led_period_moving", 0.1))
        self.period_free_breathe = float(self._declare_get("led_period_free_breathe", 2.0))

        # --- HW NeoPixel via SPI (GPIO10 MOSI, GPIO11 SCLK) ---
        self.spi = board.SPI()
        self.pixels = neopixel.NeoPixel_SPI(self.spi, self.num_pixels,
                                            brightness=self.brightness, auto_write=False)

        # --- Stato runtime ---
        self._tracks: List[dict] = []
        self._last_tracks_t = 0.0
        self._last_seen_any_track = 0.0
        self._last_scan_t = 0.0
        self._state = LedState.CONFIG
        self._last_state_change = time.monotonic()

        # --- Subscriptions & timer ---
        qos_scan = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                              history=HistoryPolicy.KEEP_LAST)
        self.sub_scan = self.n.create_subscription(LaserScan, "/scan", self._on_scan, qos_scan)
        self.sub_tracks = self.n.create_subscription(RosString, "/lam/tracks", self._on_tracks, 10)
        self.timer = self.n.create_timer(0.05, self._on_timer)

        self.n.get_logger().info(
            f"LED ready n={self.num_pixels} bright={self.brightness} move_thr={self.v_move} m/s"
        )

    # ----------- ROS param helpers -----------
    def _declare_get(self, name, default):
        self.n.declare_parameter(name, default)
        return self.n.get_parameter(name).value

    # ----------- Callbacks -----------
    def _on_scan(self, _msg: LaserScan):
        self._last_scan_t = time.monotonic()

    def _on_tracks(self, msg: RosString):
        self._last_tracks_t = time.monotonic()
        try:
            data = json.loads(msg.data)
            tracks = data.get("tracks", [])
            for t in tracks:
                if "speed" not in t:
                    vx = float(t.get("vx", 0.0)); vy = float(t.get("vy", 0.0))
                    t["speed"] = math.hypot(vx, vy)
            self._tracks = tracks
            if tracks:
                self._last_seen_any_track = self._last_tracks_t
        except Exception as e:
            self.n.get_logger().warn(f"/lam/tracks JSON non valido: {e}")
            self._tracks = []

    # ----------- Stato LED -----------
    def _compute_state(self) -> LedState:
        now = time.monotonic()
        if now - self._last_scan_t > self.stale_scan_sec:
            return LedState.CONFIG
        if any(float(t.get("speed", 0.0)) >= self.v_move for t in self._tracks):
            return LedState.MOVING
        if len(self._tracks) > 0:
            return LedState.STATIC
        if now - self._last_seen_any_track < self.free_transition_sec:
            return LedState.FREE_PENDING
        return LedState.FREE

    # ----------- HW helpers -----------
    def _fill(self, rgb: Tuple[int,int,int], scale=1.0):
        r,g,b = rgb
        r = int(max(0, min(255, r*scale)))
        g = int(max(0, min(255, g*scale)))
        b = int(max(0, min(255, b*scale)))
        self.pixels.fill((r,g,b)); self.pixels.show()

    def _off(self):
        self.pixels.fill((0,0,0)); self.pixels.show()

    # ----------- Timer animazione -----------
    def _on_timer(self):
        if not self.enabled:
            return
        now = time.monotonic()
        new_state = self._compute_state()
        if new_state != self._state and (now - self._last_state_change) >= 0.25:
            self._state = new_state
            self._last_state_change = now

        if self._state == LedState.CONFIG:
            # arancione, blink lento
            if (int(now / self.period_config) % 2) == 0: self._fill(self.color_config)
            else: self._off()
        elif self._state == LedState.FREE_PENDING:
            # giallo, blink breve
            if (int(now / 0.4) % 2) == 0: self._fill(self.color_free_pending)
            else: self._off()
        elif self._state == LedState.FREE:
            # verde “respiro” (cosine, 0.2..1.0)
            T = self.period_free_breathe
            phase = (now % T) / T
            scale = 0.2 + 0.8 * 0.5 * (1 - math.cos(2*math.pi*phase))
            self._fill(self.color_free, scale=scale)
        elif self._state == LedState.STATIC:
            # arancione, blink medio
            if (int(now / self.period_static) % 2) == 0: self._fill(self.color_static)
            else: self._off()
        elif self._state == LedState.MOVING:
            # rosso, blink veloce
            if (int(now / self.period_moving) % 2) == 0: self._fill(self.color_moving)
            else: self._off()

    # ----------- Shutdown pulito -----------
    def shutdown(self):
        if self.enabled:
            try: self._off()
            except Exception: pass
