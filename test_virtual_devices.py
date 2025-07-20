import time
import socket
import requests
import pytest

from virtual_devices import (
    VirtualDMXDevice,
    VirtualHueDevice,
    VirtualHueBridge,
    Hue2DMXBridgeDevice,
    rgb_to_xy,
    xy_to_rgb,
)
from python_hue_v2 import bridge as hue_bridge


def get_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_dmx_device_channel_setting():
    dmx = VirtualDMXDevice()
    dmx.set_channel(1, 128)
    assert dmx.channels[0] == 128

    with pytest.raises(ValueError):
        dmx.set_channel(0, 100)
    with pytest.raises(ValueError):
        dmx.set_channel(513, 0)
    with pytest.raises(ValueError):
        dmx.set_channel(1, -1)
    with pytest.raises(ValueError):
        dmx.set_channel(1, 256)


def test_dmx_device_multi_channels():
    dmx = VirtualDMXDevice()
    dmx.set_channels({1: 10, 2: 20})
    assert dmx.get_channel(1) == 10
    assert dmx.get_channel(2) == 20
    with pytest.raises(ValueError):
        dmx.get_channel(513)


def test_dmx_device_address():
    dmx = VirtualDMXDevice(address=10)
    assert dmx.address == 10
    dmx.set_address(20)
    assert dmx.address == 20
    with pytest.raises(ValueError):
        dmx.set_address(0)


def test_rgb_xy_roundtrip():
    samples = [
        (0.3, 0.3, 50),
        (0.1, 0.2, 75),
        (0.7, 0.3, 100),
    ]

    for x, y, bri in samples:
        r, g, b = xy_to_rgb(x, y, bri)
        rx, ry, _ = rgb_to_xy(r, g, b)
        assert pytest.approx(rx, abs=0.15) == x
        assert pytest.approx(ry, abs=0.15) == y


def test_hue_bridge_and_device():
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    bridge.start()
    time.sleep(0.5)  # allow server to start

    device = VirtualHueDevice(
        bridge_ip=f"127.0.0.1:{port}", auth_token="token", device_id="1", scheme="https"
    )
    resp = device.set_state(on=True, brightness=75, xy=[0.1, 0.2])
    assert resp.status_code == 200

    r = requests.get(
        f"https://127.0.0.1:{port}/clip/v2/resource/light/1", verify=False
    )
    data = r.json()["data"][0]

    assert data["id"] == "1"
    assert data["on"] is True
    assert data["dimming"]["brightness"] == 75
    assert pytest.approx(data["color"]["xy"]["x"], rel=1e-6) == 0.1
    assert pytest.approx(data["color"]["xy"]["y"], rel=1e-6) == 0.2


def test_python_hue_v2_library():
    port = get_free_port()
    bridge_server = VirtualHueBridge(port=port)
    bridge_server.start()
    time.sleep(0.5)

    br = hue_bridge.Bridge(ip_address=f"127.0.0.1:{port}", hue_application_key="token")
    result = br.set_light("2", "dimming", {"brightness": 42})
    assert result["id"] == "2"
    assert result["dimming"]["brightness"] == 42


def test_hue2dmx_bridge_device():
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    dmx = VirtualDMXDevice(address=1)

    def mapper(r: int, g: int, b: int) -> dict:
        return {0: r, 1: g, 2: b}

    Hue2DMXBridgeDevice(dmx, bridge, "3", mapper)
    bridge.start()
    time.sleep(0.5)

    payload = {
        "on": {"on": True},
        "dimming": {"brightness": 100},
        "color": {"xy": {"x": 0.7, "y": 0.298}},
    }

    r = requests.put(
        f"https://127.0.0.1:{port}/clip/v2/resource/light/3",
        json=payload,
        verify=False,
    )
    assert r.status_code == 200
    assert dmx.channels[0] > dmx.channels[1] and dmx.channels[0] > dmx.channels[2]


def test_entertainment_api():
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    dmx = VirtualDMXDevice(address=1)

    def mapper(r: int, g: int, b: int) -> dict:
        return {0: r, 1: g, 2: b}

    Hue2DMXBridgeDevice(dmx, bridge, "4", mapper)
    bridge.start()
    time.sleep(0.5)

    requests.put(
        f"https://127.0.0.1:{port}/clip/v2/entertainment_configuration/default",
        json={"action": "start"},
        verify=False,
    )
    time.sleep(0.1)

    device = VirtualHueDevice(
        bridge_ip=f"127.0.0.1:{port}", auth_token="token", device_id="4", scheme="https"
    )
    device.set_state(on=True, brightness=100, xy=[0.2, 0.1], use_entertainment=True)
    time.sleep(0.2)
    assert sum(dmx.channels[:3]) > 0


def test_hue_device_entertainment():
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    bridge.register_light("5")
    bridge.start()
    time.sleep(0.5)

    requests.put(
        f"https://127.0.0.1:{port}/clip/v2/entertainment_configuration/default",
        json={"action": "start"},
        verify=False,
    )

    device = VirtualHueDevice(
        bridge_ip=f"127.0.0.1:{port}", auth_token="token", device_id="5", scheme="https"
    )
    resp = device.set_state(on=True, brightness=60, xy=[0.3, 0.3], use_entertainment=True)
    assert resp.status_code == 200
    time.sleep(0.2)
    r = requests.get(
        f"https://127.0.0.1:{port}/clip/v2/resource/light/5", verify=False
    )
    assert r.status_code == 200


def test_huestream_rate_limiting(monkeypatch):
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    dmx = VirtualDMXDevice(address=1)

    def mapper(r: int, g: int, b: int) -> dict:
        return {0: r}

    Hue2DMXBridgeDevice(dmx, bridge, "6", mapper)
    bridge.start()
    time.sleep(0.5)

    requests.put(
        f"https://127.0.0.1:{port}/clip/v2/entertainment_configuration/default",
        json={"action": "start"},
        verify=False,
    )

    updates = 0

    def wrapped(offset: int, value: int):
        nonlocal updates
        updates += 1
        VirtualDMXDevice.set_relative_channel(dmx, offset, value)

    monkeypatch.setattr(dmx, "set_relative_channel", wrapped)

    device = VirtualHueDevice(
        bridge_ip=f"127.0.0.1:{port}", auth_token="token", device_id="6", scheme="https"
    )

    for _ in range(10):
        device.set_state(on=True, brightness=100, xy=[0.2, 0.1], use_entertainment=True)
        time.sleep(0.01)

    time.sleep(0.2)
    assert updates <= 6


def test_hue_device_context_manager():
    port = get_free_port()
    bridge = VirtualHueBridge(port=port)
    bridge.register_light("7")
    bridge.start()
    time.sleep(0.5)

    with VirtualHueDevice(
        bridge_ip=f"127.0.0.1:{port}",
        auth_token="token",
        device_id="7",
        scheme="https",
    ) as device:
        resp = device.set_state(on=True, brightness=10)
        assert resp.status_code == 200

    assert device._sock.fileno() == -1

