# auto-rec-pi

Sound-activated audio recorder for the Raspberry Pi using the [Pimoroni Pirate Audio Dual Mic](https://shop.pimoroni.com/products/pirate-audio-dual-mic) HAT. Like a motion-activated security camera, but for audio -- it continuously monitors the microphone and automatically records when sound exceeds a threshold, then stops after silence returns.

Recordings are saved as timestamped stereo WAV files and the whole system starts unattended on boot.

## Hardware

- Raspberry Pi (tested on Pi 5)
- [Pimoroni Pirate Audio Dual Mic](https://shop.pimoroni.com/products/pirate-audio-dual-mic) -- includes:
  - ADAU7002 dual MEMS microphone
  - ST7789 240x240 SPI LCD display
  - Four GPIO buttons (A, B, X, Y)

## How It Works

1. The microphone stream is always open, calculating RMS audio levels in real time.
2. A rolling **pre-buffer** (1 second by default) keeps recent audio in memory so the onset of a sound is never lost.
3. When the RMS level crosses the **trigger threshold**, a new WAV file is created and the pre-buffer is flushed into it. Recording continues as long as sound persists.
4. After a configurable **silence timeout** (default 3 s) below the threshold, the recording stops and the file is saved.
5. Recordings shorter than a minimum duration (default 1 s) are automatically discarded to avoid junk files from transient clicks.
6. A **storage cap** (default 2 GB) automatically deletes the oldest recordings when exceeded.

All of this is shown in real time on the LCD: status, level meter with threshold marker, waveform history, VU meters, clip count, and free disk space.

## Features

- Sound-activated recording with configurable sensitivity (LOW / MED / HIGH)
- 1-second audio pre-buffer captures the onset of each trigger event
- Timestamped WAV files: `rec-YYYY-MM-DD_HH-MM-SS.wav`
- Auto-deletes oldest recordings when storage limit is exceeded
- Real-time LCD display: status, level meter, waveform, VU meters, disk stats
- Manual record override via hardware button
- Graceful shutdown via button or SIGTERM (systemd-friendly)
- Starts automatically on boot via systemd

## Button Controls

| Button | GPIO | Function |
|--------|------|----------|
| **A** | 5 | Arm / Disarm auto-recording |
| **B** | 6 | Cycle sensitivity (LOW / MED / HIGH) |
| **X** | 16 | Manual record / stop |
| **Y** | 24 | Shutdown |

## Quick Start

An install script handles everything: SPI, system packages, device tree overlay, ALSA configuration, Python virtual environment, and the systemd service.

```bash
git clone <repo-url> ~/auto-rec-pi
cd ~/auto-rec-pi
./install.sh
sudo reboot
```

After reboot the recorder starts automatically. That's it.

Check status and logs:

```bash
sudo systemctl status autorecord.service
journalctl -u autorecord.service -f
```

## Manual Setup

If you prefer to set things up step by step rather than using `install.sh`:

### 1. Enable SPI

```bash
sudo raspi-config nonint do_spi 0
```

### 2. Enable the microphone overlay

Add to `/boot/firmware/config.txt` (or `/boot/config.txt` on older images):

```
dtoverlay=adau7002-simple
```

Reboot after this change.

### 3. Install system packages

```bash
sudo apt update
sudo apt install -y \
  ladspa-sdk \
  invada-studio-plugins-ladspa \
  libportaudio2 \
  python3-pip \
  python3-rpi.gpio \
  python3-spidev \
  python3-numpy \
  python3-pil \
  python3-venv
```

### 4. Configure ALSA

Copy the included ALSA config to set up the `mic_out` virtual device, which routes the raw microphone through a high-pass filter with 30 dB gain:

```bash
cp asoundrc ~/.asoundrc
```

Verify the mic works:

```bash
arecord -Dmic_out -c2 -r48000 -fS32_LE -twav -d5 -Vstereo test.wav
```

### 5. Python environment

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 6. Run manually

```bash
cd scripts
python autorecord.py
```

### 7. Set up auto-start (systemd)

Create `/etc/systemd/system/autorecord.service`:

```ini
[Unit]
Description=auto-rec-pi Sound-Activated Recorder
After=multi-user.target sound.target

[Service]
Type=simple
User=koh
WorkingDirectory=/home/koh/auto-rec-pi/scripts
ExecStart=/home/koh/auto-rec-pi/.venv/bin/python autorecord.py
Restart=on-failure
RestartSec=5
Environment=HOME=/home/koh

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now autorecord.service
```

## Configuration

All tunables are constants at the top of `scripts/autorecord.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `DEVICE` | `"mic_out"` | ALSA input device name |
| `SAMPLERATE` | `48000` | Sample rate in Hz |
| `CHANNELS` | `2` | Number of audio channels |
| `OUTPUT_DIR` | `~/auto-rec-pi/recs` | Where recordings are saved |
| `SENSITIVITY_PRESETS` | LOW/MED/HIGH | RMS thresholds for each level |
| `DEFAULT_SENSITIVITY` | `1` (MED) | Starting sensitivity index |
| `SILENCE_TIMEOUT_S` | `3.0` | Seconds of silence before stopping |
| `MIN_RECORDING_S` | `1.0` | Minimum recording length (shorter is discarded) |
| `MAX_RECORDING_S` | `3600.0` | Maximum recording length (force-stop) |
| `PRE_BUFFER_S` | `1.0` | Seconds of audio kept before trigger |
| `MAX_STORAGE_MB` | `2048` | Storage cap; oldest files are deleted beyond this |

## ALSA Pipeline

The `asoundrc` file defines a chain of ALSA plugins that process the raw mic input:

```
mic_hw  -->  mic_rt  -->  mic_plug  -->  mic_filter  -->  mic_out
(raw hw)    (routing)     (format)      (HP filter     (final
                                         + 30dB gain)   device)
```

The high-pass filter (50 Hz cutoff via Invada LADSPA) removes DC bias and low-frequency rumble, while the 30 dB gain boosts the otherwise quiet MEMS microphone signal.

## Project Structure

```
auto-rec-pi/
├── scripts/
│   ├── autorecord.py       # sound-activated recorder (main)
│   └── cliprecord.py       # manual button-based recorder (original)
├── resources/
│   ├── background.png      # LCD background for cliprecord.py
│   └── controls.png        # button overlay mask for cliprecord.py
├── asoundrc                # ALSA mic configuration
├── install.sh              # one-step setup script
├── requirements.txt        # Python dependencies
├── recs/                   # recordings directory (git-ignored)
├── LICENSE                 # MIT
└── README.md
```

## Troubleshooting

**"Device mic_out not found"** -- The ALSA config is not loaded. Make sure `~/.asoundrc` exists and contains the mic pipeline config. Run `aplay -L` to list available devices.

**No sound detected / threshold never triggers** -- Test with `arecord -Dmic_out -c2 -r48000 -fS32_LE -twav -d5 -Vstereo test.wav` and check the VU meter. If silent, verify `dtoverlay=adau7002-simple` is in your boot config and you have rebooted.

**Service fails on boot** -- Check logs with `journalctl -u autorecord.service -e`. Common issues: SPI not enabled, missing Python packages, or the `.venv` not built.

**Display is blank** -- Ensure SPI is enabled (`sudo raspi-config nonint do_spi 0`) and the HAT is seated properly.

## License

MIT
