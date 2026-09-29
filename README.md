# NVIDIA GPU fan curve without Xorg

An open-source, temperature-driven fan controller for Linux NVIDIA GPUs. It
uses NVIDIA's **NVML API directly** to set fan speeds, so it does not need an X
server, Coolbits, `nvidia-settings`, a desktop session, or a display. One
systemd service starts it at boot. It discovers **one or more GPUs**, including
cards with multiple fans, or you can select particular GPUs by UUID.

This project was developed on a Lenovo P720 with two Quadro RTX 6000 cards and
NVIDIA driver 595.91.07. Your GPU/driver must implement the relevant NVML fan
APIs; merely having `nvidia-smi` does **not** guarantee writable fan controls.
No GPU VBIOS is changed.

## Why use it?

NVIDIA's automatic fan policy is the safe default. It may favor lower fan
noise over lower temperatures, which on some sustained workloads can reduce
thermal headroom and performance. On the author's dual-RTX-6000 LLM test, the
default curve held roughly **84/87 °C**; the example curve below held about
**75/80 °C** at the same ~250 W per GPU. These are examples, not promises for
other cards. Check GPU utilization, clocks, and throttle reasons before
attributing performance loss to temperature alone.

## Requirements

- Linux with systemd, Python 3.9+, and a working proprietary NVIDIA driver
  (`nvidia-smi` works); NVML must be installed as `libnvidia-ml.so.1`.
- GPU(s) with NVML-controllable fans. `nvmlDeviceGetNumFans`,
  `nvmlDeviceGetMinMaxFanSpeed`, `nvmlDeviceSetFanSpeed_v2`, and
  `nvmlDeviceSetDefaultFanSpeed_v2` must work for your card and driver.
- Root for fan writes and for the systemd service.

The code uses only the Python standard library (`ctypes`); there are no pip
dependencies. It discovers all NVML-supported GPUs with fans by default,
skipping fanless/unsupported GPUs. If no controllable cards remain, it exits
with an error rather than silently claiming success.

## Install

Clone the repo, then from its directory:

```bash
python3 -m unittest discover -s tests -v
sudo install -m 0755 nvml-fan-curve.py /usr/local/bin/nvml-fan-curve.py
sudo install -m 0644 nvml-fan-curve.service /etc/systemd/system/nvml-fan-curve.service
sudo install -m 0644 nvml-fan-curve.conf.example /etc/nvml-fan-curve.conf
sudoedit /etc/nvml-fan-curve.conf             # review/tune curve before starting
sudo systemctl daemon-reload
sudo systemctl enable --now nvml-fan-curve.service
```

**Stop other fan controllers before starting this service.** Two controllers
writing competing manual fan targets will fight each other. Existing Xorg fan
scripts and desktop fan-control utilities need to be disabled, not necessarily
the rest of your desktop. This application does not require or stop Xorg.

Check the result:

```bash
systemctl status nvml-fan-curve.service
sudo journalctl -u nvml-fan-curve.service -b --no-pager
python3 /usr/local/bin/nvml-fan-curve.py --status
nvidia-smi --query-gpu=index,temperature.gpu,fan.speed --format=csv
```

`--status` displays NVML's *reported* fan speed percentage and target, not a
physical tachometer RPM reading. Fan speed may lag a changed target.

For a boot check, reboot when convenient and repeat the commands above. The
service is enabled, so it re-applies the curve automatically after boot.

### Upgrade / uninstall

To update, replace `/usr/local/bin/nvml-fan-curve.py` and the service file,
then run `sudo systemctl daemon-reload` and
`sudo systemctl restart nvml-fan-curve.service`.

To return to NVIDIA's automatic fan policy:

```bash
sudo systemctl disable --now nvml-fan-curve.service
python3 /usr/local/bin/nvml-fan-curve.py --status    # policy should read auto
```

Stopping the service explicitly restores the automatic policy. If needed,
`sudo python3 /usr/local/bin/nvml-fan-curve.py --restore` does so manually.
The optional config file and installed script can then be removed.

## Configuration

Edit `/etc/nvml-fan-curve.conf` and restart the service. It is a **systemd
EnvironmentFile**, not shell code. Defaults without a config file are shown:

```ini
GPUS=all
CURVE="55:40 60:50 65:60 70:72 75:85 80:100"
POLL=5
DEADBAND=4
```

- `GPUS=all` discovers every GPU with controllable fans. For a subset use
  `GPUS="0 2"` or stable UUIDs:
  `GPUS="GPU-xxxx GPU-yyyy"`. Get full UUIDs with
  `nvidia-smi --query-gpu=index,uuid,name --format=csv`. A missing selected
  device or one without controllable fans causes an error; indexes can change
  after hardware changes, so UUIDs are preferable for long-term selection.
- `CURVE` is ordered `temperature-in-C:fan-target-percent` points. It is a
  **step curve**, not interpolation. With the example above, 55–59 °C sets
  40%, 60–64 °C sets 50%, 65–69 °C sets 60%, etc.; below 55 °C it stays
  at 40%. The values are clamped to the card's NVML-reported settable range
  (33–100% on the author's RTX 6000 cards). Each GPU uses its own temperature.
- `POLL` is seconds between readings (must be positive).
- `DEADBAND` is the minimum percentage change in *curve target* before a new
  write. The script still detects a policy reset or an externally changed
  manual target and corrects it at the next poll. With the example points,
  each step is at least 10%, so the default deadband doesn't hide transitions.

For manual diagnosis (while the service is **stopped**):

```bash
sudo systemctl stop nvml-fan-curve.service
sudo /usr/local/bin/nvml-fan-curve.py --set 70
python3 /usr/local/bin/nvml-fan-curve.py --status
sudo /usr/local/bin/nvml-fan-curve.py --restore
sudo systemctl start nvml-fan-curve.service
```

`--set` applies the same target to each selected fan and clamps it to each
GPU's reported range. While the service runs it will overwrite manual changes
within `POLL` seconds. `--restore` returns control to NVIDIA's automatic
curve; the service will reassert manual control on its next poll if running.

## How it works and recovery behavior

1. NVML enumerates installed GPUs. For each selected card it discovers the
   number of fans and writable percentage range. A card with multiple fans
   gets one command per fan; each fan follows its GPU temperature.
2. Every `POLL` seconds the daemon queries the GPU temperature, then each
   fan's current policy and target. It sets a new target when the curve moves,
   a fan switches back to auto, or another client changes its manual target.
3. On a normal service stop or SIGTERM, it calls
   `nvmlDeviceSetDefaultFanSpeed_v2` for **every selected fan**; `ExecStop`
   separately attempts the same restoration. `Restart=always` restarts it
   after unexpected exits. If forcibly killed (SIGKILL), the last manual
   target can remain temporarily until restart. During an OS/driver shutdown
   or reset, NVIDIA's built-in firmware control provides the fallback.
4. The fan override is **runtime state**, not stored in VBIOS. systemd is what
   persists this *software policy* across reboots by reapplying it.

The service runs as root because NVML fan writes require privileged access.
The actual thermal safety policy remains in the card firmware. Avoid manually
forcing a low fixed speed under sustained load.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `no GPUs with NVML-controllable fans found` | Check `nvidia-smi`, GPU fans, proprietary driver and whether this GPU implements these NVML APIs. Passive/data-center GPUs often have no onboard fans. |
| `Not Supported` on set | NVML can report fan status even when the hardware/driver doesn't allow writes. Use the NVIDIA/default firmware curve or another supported interface for that card. |
| `No Permission` on set | Run the service as root; check `systemctl status nvml-fan-curve`. |
| Fans jump between targets | Check for other fan control software and tune step thresholds/deadband. |
| Target differs briefly from reported speed | Fans take time to ramp. `nvmlDeviceGetFanSpeed_v2` reports a percentage, not necessarily a physical RPM measurement. |
| One GPU ignored | Check `GPUS` and UUIDs; if `GPUS=all`, journal messages show unsupported cards being skipped. |
| Unexpected exit | Inspect `journalctl -u nvml-fan-curve.service -b`; systemd restarts after 10 s. |

NVIDIA's [NVML Device Commands documentation](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
describes `nvmlDeviceSetFanSpeed_v2` and the default-policy restore call.

## License

MIT; see [LICENSE](LICENSE).
