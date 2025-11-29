# led_manager.py
import json
import math
import time
from enum import Enum
from typing import Tuple, List, Dict

from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from rcl_interfaces.msg import SetParametersResult
from std_msgs.msg import String as RosString
from sensor_msgs.msg import LaserScan

# Backend HW (NeoPixel via SPI)
try:
    import board
    import neopixel_spi as neopixel  # adafruit-circuitpython-neopixel-spi
    try:
        from digitalio import DigitalInOut, Direction
    except Exception:
        DigitalInOut = None
        Direction = None
    _HAVE_HW = True
except Exception as e:
    _HAVE_HW = False
    _HW_ERR = e
    DigitalInOut = None
    Direction = None


class LedState(Enum):
    CONFIG = 0        # nessuno scan recente → attesa/configurazione
    FREE_PENDING = 1  # appena liberata → giallo transitorio
    FREE = 2          # libera stabile → verde “respiro”
    STATIC = 3        # ID presenti ma lenti / verificati
    MOVING = 4        # almeno un ID veloce NON verificato


class LedManager:
    """
    Gestione LED NeoPixel via SPI con stati dinamici.

    • Stati base: CONFIG/FREE_PENDING/FREE/STATIC/MOVING
    • TAG UWB (/lam/uwb_tags) con TTL locale e verifica cluster↔tag.
    • Flash ciano breve quando cambia il numero di aree (/lam/areas).
    • Modalità IDLE: se non arrivano dati (scan/tracks/tag) per > led_idle_timeout_sec → respiro turchese.
    • Prima che la sorveglianza sia armata (da Web via WS) i LED fanno una sequenza "OmniGate".
    • Buzzer attivo (pin configurabile) che suona solo in stato MOVING quando i LED sono rossi.
    """
    def __init__(self, node: Node):
        self.n = node
        self.enabled = _HAVE_HW
        if not self.enabled:
            self.n.get_logger().warn(f"LED disabilitati (HW non disponibile): {_HW_ERR}")
            return

        # --- Parametri ROS (prefisso led_) ---
        self.num_pixels = int(self._declare_get("led_num_pixels", 20))
        self.brightness = float(self._declare_get("led_brightness", 0.5))
        self.v_move = float(self._declare_get("led_moving_speed_threshold", 0.15))   # m/s
        self.stale_scan_sec = float(self._declare_get("led_stale_scan_sec", 2.0))    # → CONFIG
        self.free_transition_sec = float(self._declare_get("led_free_transition_sec", 2.0))

        # Colori (RGB)
        self.color_config = tuple(self._declare_get("led_color_config", [255, 100, 0]))             # arancione
        self.color_free = tuple(self._declare_get("led_color_free", [0, 255, 50]))                  # verde
        self.color_free_pending = tuple(self._declare_get("led_color_free_pending", [255, 200, 0])) # giallo
        self.color_static = tuple(self._declare_get("led_color_static", [255, 140, 0]))             # arancione vivo
        self.color_moving = tuple(self._declare_get("led_color_moving", [255, 0, 0]))               # rosso

        # Colore per tracce verificate da TAG
        self.color_verified = tuple(self._declare_get("led_color_verified", [0, 255, 180]))         # verde acqua

        # Parametri verifica TAG↔cluster
        self.tag_assoc_dist = float(self._declare_get("led_tag_assoc_dist", 0.25))                  # m
        self.verify_mode = str(self._declare_get("led_verify_mode", "both"))                        # centroid_radius|bbox|both
        self.verify_bbox_margin = float(self._declare_get("led_verify_bbox_margin", 0.02))          # m
        self.tag_ttl_sec = float(self._declare_get("led_tag_ttl_sec", 0.6))                         # s (TTL locale)

        # Flash cambio aree
        self.area_flash_sec = float(self._declare_get("led_area_flash_sec", 1.2))
        self.color_area_flash = tuple(self._declare_get("led_color_area_flash", [0, 200, 255]))     # ciano

        # Modalità IDLE (respiro turchese)
        self.idle_timeout_sec = float(self._declare_get("led_idle_timeout_sec", 15.0))
        self.color_idle = tuple(self._declare_get("led_color_idle", [0, 220, 255]))                 # turchese
        self.period_idle_breathe = float(self._declare_get("led_period_idle_breathe", 2.4))

        # Periodi (s)
        self.period_config = float(self._declare_get("led_period_config", 0.6))
        self.period_static = float(self._declare_get("led_period_static", 0.3))
        self.period_moving = float(self._declare_get("led_period_moving", 0.1))
        self.period_free_breathe = float(self._declare_get("led_period_free_breathe", 2.0))

        # Isteresi per MOVING (soglia alta per entrare, bassa per uscire)
        self.v_enter = float(self._declare_get("led_moving_enter", self.v_move + 0.05))
        self.v_exit = float(self._declare_get("led_moving_exit", max(0.0, self.v_move - 0.05)))
        self._moving_latched = False

        # --- HW NeoPixel via SPI (GPIO10 MOSI, GPIO11 SCLK) ---
        try:
            self.spi = board.SPI()
            self.pixels = neopixel.NeoPixel_SPI(
                self.spi, self.num_pixels, brightness=self.brightness, auto_write=False
            )
            self.pixels.show()
        except Exception as e:
            self.enabled = False
            self.n.get_logger().warn(f"LED disabilitati (init fallita): {e}")
            return

        # --- Buzzer attivo opzionale ---
        self._buzzer = None
        self._buzzer_enabled = False
        try:
            if DigitalInOut is not None:
                buzzer_pin_name = str(self._declare_get("buzzer_pin", "D18"))  # GPIO18 su Raspberry
                buzzer_pin = getattr(board, buzzer_pin_name)
                self._buzzer = DigitalInOut(buzzer_pin)
                self._buzzer.direction = Direction.OUTPUT
                self._buzzer.value = False
                self._buzzer_enabled = True
                self.n.get_logger().info(f"Buzzer inizializzato su {buzzer_pin_name}")
            else:
                self.n.get_logger().warn("digitalio non disponibile: buzzer disabilitato")
        except Exception as e:
            self.n.get_logger().warn(f"Buzzer non inizializzato: {e}")
            self._buzzer_enabled = False
            self._buzzer = None

        # --- Stato runtime ---
        self._tracks: List[dict] = []
        self._last_tracks_t = 0.0
        self._last_seen_any_track = 0.0
        self._last_scan_t = 0.0
        self._state = LedState.CONFIG
        self._last_state_change = time.monotonic()

        # TAG UWB locali: id -> {'x','y','stamp_mono'}
        self._tags: Dict[str, Dict[str, float]] = {}

        # Aree → flash feedback
        self._last_area_count = -1
        self._flash_until = 0.0

        # Attività complessiva (scan/tracks/tag). Serve per modalità IDLE.
        now0 = time.monotonic()
        self._last_activity_t = now0

        # Cache ultimo output LED (per evitare show() inutili)
        self._last_rgb = (0, 0, 0)
        self._last_scale = -1.0

        # Sorveglianza armata (abilitata da WebSocket via RosBridge)
        self.surveillance_armed = False

        # Palette "OmniGate" pre-arm (cyan → violet)
        self.omni_palette: List[Tuple[int, int, int]] = [
            (0, 220, 255),
            (64, 196, 255),
            (128, 160, 255),
            (180, 120, 255),
            (210, 100, 255),
        ]
        self.omni_step = 0.35 # 0.35  secondi per colore

        # --- Subscriptions & timer ---
        qos_scan = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.sub_scan = self.n.create_subscription(LaserScan, "/scan", self._on_scan, qos_scan)
        self.sub_tracks = self.n.create_subscription(RosString, "/lam/tracks", self._on_tracks, 10)

        # TAG UWB (non latched)
        self.sub_tags = self.n.create_subscription(RosString, "/lam/uwb_tags", self._on_tags, 10)

        # Aree (latched) per feedback flash
        qos_lat = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.sub_areas = self.n.create_subscription(RosString, "/lam/areas", self._on_areas, qos_lat)

        self.timer = self.n.create_timer(0.05, self._on_timer)

        # Param callback per aggiornare a caldo
        self.n.add_on_set_parameters_callback(self._on_params)

        self.n.get_logger().info(
            f"LED ready n={self.num_pixels} bright={self.brightness} "
            f"move_thr={self.v_move} m/s (enter={self.v_enter} / exit={self.v_exit})"
        )

    # ----------- ROS param helpers -----------
    def _declare_get(self, name, default):
        self.n.declare_parameter(name, default)
        return self.n.get_parameter(name).value

    def _on_params(self, params):
        changed = False
        try:
            for p in params:
                if p.name == "led_moving_speed_threshold":
                    self.v_move = float(p.value); changed = True
                elif p.name == "led_stale_scan_sec":
                    self.stale_scan_sec = float(p.value); changed = True
                elif p.name == "led_free_transition_sec":
                    self.free_transition_sec = float(p.value); changed = True
                elif p.name == "led_period_config":
                    self.period_config = float(p.value); changed = True
                elif p.name == "led_period_static":
                    self.period_static = float(p.value); changed = True
                elif p.name == "led_period_moving":
                    self.period_moving = float(p.value); changed = True
                elif p.name == "led_period_free_breathe":
                    self.period_free_breathe = float(p.value); changed = True
                elif p.name == "led_color_config":
                    self.color_config = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_color_free":
                    self.color_free = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_color_free_pending":
                    self.color_free_pending = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_color_static":
                    self.color_static = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_color_moving":
                    self.color_moving = tuple(int(x) for x in p.value); changed = True
                # nuovi (TAG/verify/flash)
                elif p.name == "led_color_verified":
                    self.color_verified = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_tag_assoc_dist":
                    self.tag_assoc_dist = float(p.value); changed = True
                elif p.name == "led_verify_mode":
                    self.verify_mode = str(p.value); changed = True
                elif p.name == "led_verify_bbox_margin":
                    self.verify_bbox_margin = float(p.value); changed = True
                elif p.name == "led_tag_ttl_sec":
                    self.tag_ttl_sec = float(p.value); changed = True
                elif p.name == "led_area_flash_sec":
                    self.area_flash_sec = float(p.value); changed = True
                elif p.name == "led_color_area_flash":
                    self.color_area_flash = tuple(int(x) for x in p.value); changed = True
                # isteresi moving
                elif p.name == "led_moving_enter":
                    self.v_enter = float(p.value); changed = True
                elif p.name == "led_moving_exit":
                    self.v_exit = float(p.value); changed = True
                # brightness
                elif p.name == "led_brightness":
                    self.brightness = float(p.value)
                    try:
                        self.pixels.brightness = self.brightness
                    except Exception:
                        pass
                    changed = True
                # idle
                elif p.name == "led_idle_timeout_sec":
                    self.idle_timeout_sec = float(p.value); changed = True
                elif p.name == "led_color_idle":
                    self.color_idle = tuple(int(x) for x in p.value); changed = True
                elif p.name == "led_period_idle_breathe":
                    self.period_idle_breathe = float(p.value); changed = True

            if changed:
                self._last_state_change = time.monotonic()
            return SetParametersResult(successful=True)
        except Exception as e:
            self.n.get_logger().warn(f"Param update error: {e}")
            return SetParametersResult(successful=False)

    # ----------- API esterna (arm/disarm sorveglianza) -----------
    def set_armed(self, value: bool):
        """Richiamata dal bridge WebSocket quando premi il pulsante in UI."""
        self.surveillance_armed = bool(value)
        self._last_state_change = time.monotonic()
        state = "ATTIVA" if self.surveillance_armed else "DISATTIVATA"
        try:
            self.n.get_logger().info(f"Sorveglianza {state}")
        except Exception:
            pass

    # ----------- Callbacks -----------
    def _on_scan(self, _msg: LaserScan):
        nowm = time.monotonic()
        self._last_scan_t = nowm
        self._last_activity_t = nowm  # conta come nuovo dato

    def _on_tracks(self, msg: RosString):
        nowm = time.monotonic()
        self._last_tracks_t = nowm
        self._last_activity_t = nowm  # nuovo dato
        try:
            data = json.loads(msg.data)
            tracks = data.get("tracks", [])
            for t in tracks:
                if "speed" not in t:
                    vx = float(t.get("vx", 0.0))
                    vy = float(t.get("vy", 0.0))
                    t["speed"] = math.hypot(vx, vy)
            self._tracks = tracks
            if tracks:
                self._last_seen_any_track = self._last_tracks_t
        except Exception as e:
            self.n.get_logger().warn(f"/lam/tracks JSON non valido: {e}")
            self._tracks = []

    def _on_tags(self, msg: RosString):
        """Accetta JSON dal nodo o dal bridge. Usa sempre METRI e TTL locale."""
        nowm = time.monotonic()
        self._last_activity_t = nowm  # nuovo dato
        try:
            data = json.loads(msg.data)
            units = str(data.get("units", "m")).lower()
            for t in data.get("tags", []):
                tid = str(t.get("id", "")).strip()
                if not tid:
                    continue
                if "x_m" in t and "y_m" in t:
                    x = float(t["x_m"]); y = float(t["y_m"])
                else:
                    x = float(t.get("x", 0.0)); y = float(t.get("y", 0.0))
                    if units == "cm":
                        x *= 0.01; y *= 0.01
                self._tags[tid] = {"x": x, "y": y, "stamp_mono": nowm}
        except Exception as e:
            self.n.get_logger().warn(f"/lam/uwb_tags JSON non valido: {e}")

    def _on_areas(self, msg: RosString):
        """Attiva un flash breve quando cambia il numero di aree."""
        try:
            data = json.loads(msg.data)
            n_areas = len(data.get("areas", []))
            if self._last_area_count != n_areas:
                self._last_area_count = n_areas
                self._flash_until = time.monotonic() + self.area_flash_sec
        except Exception:
            pass

    # ----------- Stato LED -----------
    def _compute_state(self) -> LedState:
        """Determina lo stato globale considerando solo le tracce NON verificate per il MOVING."""
        now = time.monotonic()

        # nessuno scan recente → CONFIG (potrà essere scavalcato da IDLE / pre-arm)
        if now - self._last_scan_t > self.stale_scan_sec:
            self._moving_latched = False
            return LedState.CONFIG

        # velocità massima solo delle tracce NON verificate
        vmax_unverified = 0.0
        for t in self._tracks:
            if not self._track_verified(t):
                v = float(t.get("speed", 0.0))
                if v > vmax_unverified:
                    vmax_unverified = v

        # isteresi
        if self._moving_latched:
            if vmax_unverified <= self.v_exit:
                self._moving_latched = False
        else:
            if vmax_unverified >= self.v_enter:
                self._moving_latched = True

        if self._moving_latched:
            return LedState.MOVING
        if len(self._tracks) > 0:
            return LedState.STATIC
        if now - self._last_seen_any_track < self.free_transition_sec:
            return LedState.FREE_PENDING
        return LedState.FREE

    # ----------- Helper verifica cluster↔tag -----------
    def _track_verified(self, t: dict) -> bool:
        tx, ty = float(t.get("x", 0.0)), float(t.get("y", 0.0))
        bbox = t.get("bbox", None)

        # tag più vicino
        mind = 1e9
        near = None
        for tag in self._tags.values():
            d = math.hypot(tx - tag["x"], ty - tag["y"])
            if d < mind:
                mind = d
                near = tag

        if near is None:
            return False

        by_radius = (mind <= self.tag_assoc_dist)
        by_bbox = False
        if bbox is not None:
            minx, miny, maxx, maxy = bbox
            m = float(self.verify_bbox_margin)
            by_bbox = (minx - m <= near["x"] <= maxx + m) and (miny - m <= near["y"] <= maxy + m)

        mode = (self.verify_mode or "both").lower()
        if mode == "centroid_radius":
            return by_radius
        elif mode == "bbox":
            return by_bbox
        else:
            return by_radius or by_bbox

    # ----------- HW helpers -----------
    def _fill(self, rgb: Tuple[int, int, int], scale=1.0):
        if not self.enabled:
            return
        r, g, b = rgb
        r = int(max(0, min(255, r * scale)))
        g = int(max(0, min(255, g * scale)))
        b = int(max(0, min(255, b * scale)))

        # evita aggiornamenti inutili (eccesso di I/O)
        if (r, g, b) == self._last_rgb and (abs(scale - self._last_scale) < 0.02):
            return
        self._last_rgb = (r, g, b)
        self._last_scale = scale

        try:
            self.pixels.fill((r, g, b))
            self.pixels.show()
        except Exception as e:
            self.enabled = False
            self.n.get_logger().error(f"LED error (fill), disabilitato: {e}")

    def _off(self):
        if not self.enabled:
            return
        if self._last_rgb == (0, 0, 0) and self._last_scale == 0.0:
            return
        self._last_rgb = (0, 0, 0)
        self._last_scale = 0.0
        try:
            self.pixels.fill((0, 0, 0))
            self.pixels.show()
        except Exception as e:
            self.enabled = False
            self.n.get_logger().error(f"LED error (off), disabilitato: {e}")

    # Buzzer helpers
    def _buzz_on(self):
        if self._buzzer_enabled and self._buzzer is not None:
            try:
                self._buzzer.value = True
            except Exception:
                self._buzzer_enabled = False

    def _buzz_off(self):
        if self._buzzer_enabled and self._buzzer is not None:
            try:
                self._buzzer.value = False
            except Exception:
                self._buzzer_enabled = False

    # Pattern pre-arm "OmniGate"
    # def _pattern_omnigate(self, now: float):
    #     if not self.enabled or not self.omni_palette:
    #         self._off()
    #         return
    #     idx = int(now / self.omni_step) % len(self.omni_palette)
    #     self._fill(self.omni_palette[idx])

    # Pattern pre-arm "OmniGate" con doppio anello
    def _pattern_omnigate(self, now: float):
        if not self.enabled or self.num_pixels <= 0 or not self.omni_palette:
            self._off()
            return

        # --- layout fisico: 12 esterni + 8 interni in cascata ---
        outer_n = min(12, self.num_pixels)
        inner_n = max(0, self.num_pixels - outer_n)

        # velocità di rotazione (giri al secondo, segno = verso)
        speed_outer = 0.25    # esterno: orario
        speed_inner = -0.32   # interno: antiorario

        # respiro globale (2–3 s)
        breath_T = 3.0
        phase_breath = (now % breath_T) / breath_T
        breath = 0.35 + 0.65 * 0.5 * (1.0 - math.cos(2.0 * math.pi * phase_breath))

        def circ_dist(a: float, b: float) -> float:
            """distanza circolare su [0,1)."""
            d = abs(a - b)
            return d if d <= 0.5 else 1.0 - d

        def palette(pos: float) -> Tuple[int, int, int]:
            """Interpolazione nella palette OmniGate (ciano→viola)."""
            if not self.omni_palette:
                return (0, 0, 0)
            n = len(self.omni_palette)
            pos = max(0.0, min(0.999, pos)) * n
            i0 = int(pos) % n
            i1 = (i0 + 1) % n
            f = pos - int(pos)
            r0, g0, b0 = self.omni_palette[i0]
            r1, g1, b1 = self.omni_palette[i1]
            r = int(r0 + (r1 - r0) * f)
            g = int(g0 + (g1 - g0) * f)
            b = int(b0 + (b1 - b0) * f)
            return (r, g, b)

        # ---------- Anello esterno ----------
        if outer_n > 0:
            center = (now * speed_outer) % 1.0
            center2 = (center + 0.5) % 1.0   # seconda scia a 180°
            width = 0.22                     # “larghezza” di ogni scia

            for i in range(outer_n):
                u = i / outer_n                         # posizione [0,1)
                d1 = circ_dist(u, center)
                d2 = circ_dist(u, center2)
                w1 = max(0.0, 1.0 - d1 / width)
                w2 = max(0.0, 1.0 - d2 / width)
                bright = min(1.0, w1 + 0.7 * w2)       # due picchi, uno leggermente più debole

                # colore: gradiente che scorre piano lungo l’anello
                col = palette((u + now * 0.05) % 1.0)
                self.pixels[i] = tuple(int(c * bright * breath) for c in col)

        # ---------- Anello interno ----------
        if inner_n > 0:
            center = (now * speed_inner) % 1.0
            center2 = (center + 0.5) % 1.0
            width = 0.30

            for j in range(inner_n):
                u = j / inner_n
                d1 = circ_dist(u, center)
                d2 = circ_dist(u, center2)
                w1 = max(0.0, 1.0 - d1 / width)
                w2 = max(0.0, 1.0 - d2 / width)
                # base leggermente accesa + picchi che girano
                bright = 0.3 + 0.7 * min(1.0, w1 + w2)

                # ruoto un po’ la tinta rispetto all’anello esterno
                col = palette((u + 0.25 + now * 0.05) % 1.0)
                idx = outer_n + j  # dopo i 12 esterni
                self.pixels[idx] = tuple(int(c * bright * breath) for c in col)

        try:
            self.pixels.show()
        except Exception as e:
            self.enabled = False
            self.n.get_logger().error(
                f"LED error (OmniGate pattern), disabilitato: {e}"
            )

    # ----------- Timer animazione -----------
    def _on_timer(self):
        if not self.enabled:
            return
        now = time.monotonic()

        # TTL locale per i TAG
        cut = now - self.tag_ttl_sec
        if self._tags:
            for tid in [k for k, v in self._tags.items() if v.get("stamp_mono", 0.0) < cut]:
                self._tags.pop(tid, None)

        # piccola isteresi di stato (calcolata a prescindere)
        new_state = self._compute_state()
        if new_state != self._state and (now - self._last_state_change) >= 0.25:
            self._state = new_state
            self._last_state_change = now

        # Flash ciano breve quando cambiano le aree (priorità massima)
        if now < self._flash_until:
            # 8 Hz flash
            if (int(now / 0.12) % 2) == 0:
                self._fill(self.color_area_flash)
            else:
                self._off()
            self._buzz_off()
            return

        # Modalità IDLE: nessun dato (scan/tracks/tag) per oltre idle_timeout_sec → respiro turchese
        if (now - self._last_activity_t) > self.idle_timeout_sec:
            T = self.period_idle_breathe
            phase = (now % T) / T
            scale = 0.2 + 0.8 * 0.5 * (1 - math.cos(2 * math.pi * phase))
            self._fill(self.color_idle, scale=scale)
            self._buzz_off()
            return

        # Se la sorveglianza NON è ancora armata → pattern OmniGate
        if not self.surveillance_armed:
            self._pattern_omnigate(now)
            self._buzz_off()
            return

        # pattern di stato (sorveglianza attiva)
        if self._state == LedState.CONFIG:
            # arancione, blink lento
            if (int(now / self.period_config) % 2) == 0:
                self._fill(self.color_config)
            else:
                self._off()
            self._buzz_off()

        elif self._state == LedState.FREE_PENDING:
            # giallo, blink breve
            if (int(now / 0.4) % 2) == 0:
                self._fill(self.color_free_pending)
            else:
                self._off()
            self._buzz_off()

        elif self._state == LedState.FREE:
            # verde “respiro” (cosine 0.2..1.0)
            T = self.period_free_breathe
            phase = (now % T) / T
            scale = 0.2 + 0.8 * 0.5 * (1 - math.cos(2 * math.pi * phase))
            self._fill(self.color_free, scale=scale)
            self._buzz_off()

        elif self._state == LedState.STATIC:
            # se esiste almeno una traccia "verificata" da TAG → colore_verified
            any_verified = any(self._track_verified(t) for t in self._tracks)
            col = self.color_verified if any_verified else self.color_static
            if (int(now / self.period_static) % 2) == 0:
                self._fill(col)
            else:
                self._off()
            self._buzz_off()

        elif self._state == LedState.MOVING:
            # rosso, blink veloce + buzzer ON nella fase "on"
            if (int(now / self.period_moving) % 2) == 0:
                self._fill(self.color_moving)
                self._buzz_on()
            else:
                self._off()
                self._buzz_off()

    # ----------- Shutdown pulito -----------
    def shutdown(self):
        if self.enabled:
            try:
                self._off()
                self._buzz_off()
            except Exception:
                pass
