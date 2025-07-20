# dmx2hue

This repository contains simple Python implementations of virtual lighting devices.

## Components

- `VirtualDMXDevice` provides a minimal representation of a DMX512 device with a configurable start address.
- `VirtualHueDevice` wraps the Hue API v2 for controlling lights and can send
  updates via a simplified Hue Entertainment streaming implementation.
- `VirtualHueBridge` offers a lightweight Flask server that emulates some Hue Bridge v2 endpoints.
- `Hue2DMXBridgeDevice` exposes a DMX device as a Hue lamp using a custom RGB-to-DMX mapping. Streaming updates are throttled to the DMX refresh rate (~44 Hz).
- `xy_to_rgb(x, y, brightness)` converts Hue xy coordinates to an RGB tuple.

## Usage

Install dependencies:

```bash
pip install requests flask python-hue-v2 cryptography
```

Example usage:

```python
from virtual_devices import (
    VirtualDMXDevice,
    VirtualHueDevice,
    VirtualHueBridge,
    Hue2DMXBridgeDevice,
)

# DMX device
dmx = VirtualDMXDevice(address=1)
dmx.set_channel(1, 255)
dmx.dump_frame()

# Hue bridge
bridge = VirtualHueBridge()
bridge.start()
requests.put(
    "https://127.0.0.1:8000/clip/v2/entertainment_configuration/default",
    json={"action": "start"},
    verify=False,
)

# Map RGB directly to the first three DMX channels
Hue2DMXBridgeDevice(dmx, bridge, "3", lambda r, g, b: {0: r, 1: g, 2: b})

# Hue device
with VirtualHueDevice(
    bridge_ip="127.0.0.1:8000",
    auth_token="demo",
    device_id="1",
    scheme="https",
) as hue:
    hue.set_state(on=True, brightness=50)

    # send an Entertainment API update
    hue.set_state(on=True, brightness=80, xy=[0.5, 0.4], use_entertainment=True)
```

These classes are simplified and intended for testing or educational purposes.
The optional Entertainment API support allows basic streaming of color updates
for quick experiments without real Hue hardware. When used as a context manager,
`VirtualHueDevice` automatically closes its network socket on exit.
