import json
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

import requests
from flask import Flask, jsonify, request


def xy_to_rgb(x: float, y: float, brightness: int) -> tuple[int, int, int]:
    """Convert CIE xy coordinates and brightness to an RGB tuple."""
    if brightness <= 0 or y == 0:
        return 0, 0, 0
    z = 1.0 - x - y
    Y = brightness / 100.0
    X = (Y / y) * x
    Z = (Y / y) * z

    r = X * 1.612 - Y * 0.203 - Z * 0.302
    g = -X * 0.509 + Y * 1.412 + Z * 0.066
    b = X * 0.026 - Y * 0.072 + Z * 0.962

    r = max(0.0, r)
    g = max(0.0, g)
    b = max(0.0, b)
    max_val = max(r, g, b)
    if max_val > 1.0:
        r /= max_val
        g /= max_val
        b /= max_val

    return int(r * 255), int(g * 255), int(b * 255)


class VirtualDMXDevice:
    """Virtual representation of a DMX512 device with an assignable address."""

    CHANNELS = 512

    def __init__(self, address: int = 1) -> None:
        # DMX channels are 1-indexed. We store them as 0-indexed list.
        self.channels: List[int] = [0] * self.CHANNELS
        self.set_address(address)

    def set_address(self, address: int) -> None:
        """Configure the DMX start address of this device."""
        if not 1 <= address <= self.CHANNELS:
            raise ValueError("address must be in range 1-512")
        self.address = address

    def set_channel(self, channel: int, value: int) -> None:
        """Set a DMX channel value following the DMX512 protocol."""
        if not 1 <= channel <= self.CHANNELS:
            raise ValueError("channel must be in range 1-512")
        if not 0 <= value <= 255:
            raise ValueError("value must be 0-255")
        self.channels[channel - 1] = value

    def set_relative_channel(self, offset: int, value: int) -> None:
        """Set a channel relative to this device's start address."""
        self.set_channel(self.address + offset, value)

    def get_frame(self) -> bytes:
        """Return a DMX512 frame (start code + 512 channel values)."""
        # DMX512 uses a start code byte followed by channel data
        return bytes([0] + self.channels)

    def dump_frame(self) -> None:
        """Output the current DMX frame (for debugging)."""
        frame = self.get_frame()
        print(frame)


class VirtualHueDevice:
    """Virtual Hue device implementing basic Hue API v2 calls and simplified
    Hue Entertainment streaming over UDP."""

    class DummyResponse:
        def __init__(self, status_code: int = 200) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:  # pragma: no cover - simple stub
            pass

    def __init__(self, bridge_ip: str, auth_token: str, device_id: str, scheme: str = "http") -> None:
        self.bridge_ip = bridge_ip
        self.auth_token = auth_token
        self.device_id = device_id
        self.scheme = scheme
        self._seq = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.bridge_ip}/clip/v2/resource/light/{self.device_id}"

    @property
    def entertainment_url(self) -> str:
        return f"{self.scheme}://{self.bridge_ip}/clip/v2/entertainment/{self.device_id}"

    def set_state(
        self,
        on: Optional[bool] = None,
        brightness: Optional[int] = None,
        xy: Optional[List[float]] = None,
        use_entertainment: bool = False,
    ) -> requests.Response:
        """Set light state using Hue API v2 or stream via Entertainment."""
        if use_entertainment:
            if xy is None:
                xy = [0.0, 0.0]
            if brightness is None:
                brightness = 100
            r, g, b = xy_to_rgb(xy[0], xy[1], brightness)
            self._send_entertainment(r, g, b)
            return self.DummyResponse(200)

        data: Dict[str, Dict] = {"on": {"on": on} if on is not None else {}}
        if brightness is not None:
            data.setdefault("dimming", {})["brightness"] = brightness
        if xy is not None:
            data.setdefault("color", {})["xy"] = {"x": xy[0], "y": xy[1]}

        headers = {
            "hue-application-key": self.auth_token,
            "Content-Type": "application/json",
        }
        resp = requests.put(
            self.base_url,
            headers=headers,
            data=json.dumps(data),
            verify=False,
        )
        resp.raise_for_status()
        return resp


    def _send_entertainment(self, r: int, g: int, b: int) -> None:
        parts = self.bridge_ip.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 8000
        udp_port = port + 1000
        header = b"HueStream" + bytes([2, 0, self._seq & 0xFF, 0, 0, 0, 0])
        config_id = b"default".ljust(36, b"\x00")
        def scale(v: int) -> tuple[int, int]:
            v16 = (v << 8) | v
            return (v16 >> 8) & 0xFF, v16 & 0xFF

        r_hi, r_lo = scale(r)
        g_hi, g_lo = scale(g)
        b_hi, b_lo = scale(b)
        payload = bytes([0, r_hi, r_lo, g_hi, g_lo, b_hi, b_lo])
        msg = header + config_id + payload
        self._sock.sendto(msg, (host, udp_port))
        self._seq = (self._seq + 1) % 256


class VirtualHueBridge:
    """Minimal virtual Hue Bridge implementing Hue API v2 endpoints."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.lights: Dict[str, Dict] = {}
        self._callbacks: Dict[str, Callable[[Dict], None]] = {}
        self.channel_map: Dict[int, str] = {}
        self._configure_routes()
        self.thread: Optional[threading.Thread] = None
        self.stream_active = False
        self._udp_thread: Optional[threading.Thread] = None

    def register_light(
        self, light_id: str, callback: Optional[Callable[[Dict], None]] = None
    ) -> None:
        """Register a light with an optional update callback."""
        self.lights.setdefault(light_id, {"id": light_id})
        if callback:
            self._callbacks[light_id] = callback
        if light_id not in self.channel_map.values():
            ch_id = len(self.channel_map)
            self.channel_map[ch_id] = light_id

    def _configure_routes(self) -> None:
        def _response(data=None, errors=None, status: int = 200):
            return (
                jsonify({"data": data or [], "errors": errors or []}),
                status,
            )

        @self.app.get("/clip/v2/resource/light")
        def list_lights():
            return _response(list(self.lights.values()))

        @self.app.get("/clip/v2/resource/light/<light_id>")
        def get_light(light_id: str):
            light = self.lights.get(light_id)
            if light is None:
                return _response([], [{"description": "not found"}], 404)
            return _response([light])

        @self.app.put("/clip/v2/resource/light/<light_id>")
        def update_light(light_id: str):
            light = self.lights.setdefault(light_id, {"id": light_id})
            payload = request.get_json(force=True)
            if "on" in payload:
                light["on"] = payload["on"].get("on", light.get("on", False))
            if "dimming" in payload:
                light["dimming"] = payload["dimming"]
            if "color" in payload:
                light["color"] = payload["color"]
            cb = self._callbacks.get(light_id)
            if cb:
                cb(light)
            return _response([light])

        @self.app.get("/clip/v2/resource/entertainment_configuration")
        def list_entertainment():
            config = {
                "id": "default",
                "type": "entertainment_configuration",
                "channels": [
                    {"channel_id": cid, "position": {"x": 0, "y": 0, "z": 0}}
                    for cid in self.channel_map.keys()
                ],
                "status": "active" if self.stream_active else "inactive",
            }
            return _response([config])

        @self.app.put("/clip/v2/entertainment_configuration/<config_id>")
        def control_entertainment(config_id: str):
            payload = request.get_json(force=True)
            action = payload.get("action")
            if action == "start":
                self.stream_active = True
                self._start_udp()
            elif action == "stop":
                self.stream_active = False
            return _response([{"id": config_id, "status": "active" if self.stream_active else "inactive"}])

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self.app.run,
            kwargs={
                "host": self.host,
                "port": self.port,
                "ssl_context": "adhoc",
                "use_reloader": False,
            },
        )
        self.thread.daemon = True
        self.thread.start()
        if self.stream_active:
            self._start_udp()

    def stop(self) -> None:
        # Flask's builtin server does not support programmatic shutdown cleanly.
        pass

    def _start_udp(self) -> None:
        if self._udp_thread and self._udp_thread.is_alive():
            return
        self._udp_thread = threading.Thread(target=self._udp_server, daemon=True)
        self._udp_thread.start()

    def _udp_server(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_port = self.port + 1000
        sock.bind((self.host, udp_port))
        sock.settimeout(0.1)
        while self.stream_active:
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                continue
            self._handle_stream_data(data)
        sock.close()

    def _handle_stream_data(self, data: bytes) -> None:
        if not data.startswith(b"HueStream") or len(data) < 52:
            return
        idx = 52
        while idx + 7 <= len(data):
            ch_id = data[idx]
            r = (data[idx + 1] << 8) | data[idx + 2]
            g = (data[idx + 3] << 8) | data[idx + 4]
            b = (data[idx + 5] << 8) | data[idx + 6]
            idx += 7
            light_id = self.channel_map.get(ch_id)
            if light_id is None:
                continue
            light = self.lights.setdefault(light_id, {"id": light_id})
            light["on"] = True
            light["dimming"] = {"brightness": max(r, g, b) * 100 // 65535}
            light["color"] = {"xy": {"x": 0.0, "y": 0.0}}
            light["stream_rgb"] = (r >> 8, g >> 8, b >> 8)
            cb = self._callbacks.get(light_id)
            if cb:
                cb(light)


class Hue2DMXBridgeDevice:
    """Adapter exposing a DMX device as a Hue color lamp."""

    def __init__(
        self,
        dmx_device: VirtualDMXDevice,
        bridge: VirtualHueBridge,
        light_id: str,
        rgb_mapper: Callable[[int, int, int], Dict[int, int]],
    ) -> None:
        self.dmx_device = dmx_device
        self.rgb_mapper = rgb_mapper
        self.light_id = light_id
        self.bridge = bridge
        self._last_update = 0.0
        self._min_interval = 1.0 / 44  # DMX refresh rate ~44 Hz

        self.state: Dict = {
            "id": light_id,
            "on": False,
            "dimming": {"brightness": 100},
            "color": {"xy": {"x": 0.0, "y": 0.0}},
        }

        self.bridge.register_light(light_id, self._on_update)
        self.bridge.lights[light_id] = self.state


    def _on_update(self, state: Dict) -> None:
        if "stream_rgb" in state:
            now = time.time()
            if now - self._last_update < self._min_interval:
                return
            self._last_update = now
            r, g, b = state["stream_rgb"]
        else:
            on = state.get("on", False)
            bri = state.get("dimming", {}).get("brightness", 0)
            xy = state.get("color", {}).get("xy", {"x": 0.0, "y": 0.0})
            x = xy.get("x", 0.0)
            y = xy.get("y", 0.0)

            if on:
                r, g, b = xy_to_rgb(x, y, bri)
            else:
                r, g, b = 0, 0, 0

        channel_values = self.rgb_mapper(r, g, b)
        for offset, value in channel_values.items():
            self.dmx_device.set_relative_channel(offset, value)


