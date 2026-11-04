# Counter bridge

The counter bridge makes an IP camera the shop's counter camera. It runs on a computer on the same network as the camera: a Raspberry Pi, a mini PC or a laptop. It reads the stream with OpenCV and looks at the picture once a second. A still goes to Musha when the picture changes, and one goes at least every three minutes. A dark or blurred picture waits for the three-minute still.

Stills travel the route the counter phone uses, with the shop's counter key. Musha reads them the same way, and the owner's **Look in** reaches the bridge within five seconds.

## Connect

1. In the owner's console, press **Connect your shop**, then **An IP camera**. Copy the command. It carries the shop's counter link.
2. Install the bridge on the computer beside the camera:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip install -r requirements.txt
```

3. Find the camera's RTSP address in the table below and test it once. The first run prints the terms the manager agrees to. With the manager's yes, add `--agree` and the manager's name:

```bash
python counter_bridge.py --link "<counter link>" --camera "<rtsp address>" --agree --name "Farai" --once
```

A line such as `still sent (first): Open, two customers at the counter` means the shop can see. The owner's console shows who agreed and when.

4. Run the same command, minus `--once`, and leave it running. On Linux, systemd keeps it going after a restart:

```ini
# /etc/systemd/system/musha-bridge.service
[Unit]
Description=Musha counter bridge
After=network-online.target

[Service]
WorkingDirectory=/opt/musha-bridge
Environment=MUSHA_LINK=<counter link>
Environment=MUSHA_CAMERA=<rtsp address>
ExecStart=/opt/musha-bridge/.venv/bin/python counter_bridge.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now musha-bridge
```

## Disconnect

In **Connect your shop**, press **Disconnect**. The shop gets a new counter link, and the bridge stops with a line that says so. Connect again with the new link.

## RTSP addresses

| Camera | Address | Notes |
|---|---|---|
| Hikvision and HiLook | `rtsp://user:pass@<ip>:554/Streaming/Channels/101` | `102` is the sub stream, lighter on the network. |
| Dahua, Imou and Amcrest | `rtsp://user:pass@<ip>:554/cam/realmonitor?channel=1&subtype=0` | `subtype=1` is the sub stream. |
| Any ONVIF camera | The address ONVIF Device Manager shows under **Live video** | ONVIF Device Manager runs on Windows and asks the camera for its stream address. |
| TP-Link Tapo | `rtsp://user:pass@<ip>:554/stream1` | Create a camera account in the Tapo app first. `stream2` is the lower quality. |

The sub stream is enough: Musha keeps stills at 640 px. The bridge reads a camera on its own network.

## Try it with a video file

`--camera` also takes a video file or a webcam number such as `0`. A file plays at its own speed and loops, as a camera would:

```bash
python counter_bridge.py --link "<counter link>" --camera shop.mp4
```

## Options

| Option | Default | What it sets |
|---|---|---|
| `--look` | 1 | Seconds between looks at the picture |
| `--beat` | 5 | Seconds between heartbeats, which carry the owner's Look in |
| `--min-gap` | 8 | Seconds between stills sent on a change |
| `--every` | 180 | Seconds: a still goes at least this often |
| `--change` | 6 | Grey levels of change, on average, that send a still |

`MUSHA_LINK`, `MUSHA_CAMERA` and `MUSHA_NAME` stand in for `--link`, `--camera` and `--name`.

## The gate

The gate shrinks each picture to 32 by 24 grey pixels and compares it with the last still sent, after taking out the camera's own change in exposure. It measures brightness and sharpness on a 160 by 120 copy. The gate is a set of pure functions, tested with `python -m pytest test_gate.py`.
