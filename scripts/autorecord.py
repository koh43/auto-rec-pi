#!/usr/bin/env python3
"""Sound-activated audio recorder for Pirate Audio Dual Mic.

Continuously monitors the microphone and records WAV files when
ambient sound exceeds a configurable threshold. Works like a
motion-activated security camera, but for audio.
"""

import collections
import datetime
import math
import numpy
import pathlib
import shutil
import signal
import sounddevice
import time
import wave

from PIL import Image, ImageDraw, ImageFont
from fonts.ttf import RobotoMedium
import lgpio
from st7789 import ST7789

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEVICE = "mic_out"
SAMPLERATE = 48000
CHANNELS = 2

OUTPUT_DIR = pathlib.Path.home() / "auto-rec-pi" / "recs"

SENSITIVITY_PRESETS = [
    ("LOW", 0.10),
    ("MED", 0.06),
    ("HIGH", 0.025),
]
DEFAULT_SENSITIVITY = 2  # HIGH

NOISE_GATE = 0.008

WIND_LF_RATIO = 0.85
WIND_LF_CUTOFF = 300

LIMITER_THRESHOLD = 0.75
LIMITER_KNEE = 0.15

SILENCE_TIMEOUT_S = 8.0
MIN_RECORDING_S = 1.0
MAX_RECORDING_S = 3600.0
PRE_BUFFER_S = 2.0

MAX_STORAGE_MB = 2048

DISPLAY_FPS = 15
BUTTONS = [5, 6, 16, 24]

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

BG = (12, 12, 20)
WHITE = (210, 210, 220)
DIM = (50, 50, 65)
GREEN = (47, 173, 102)
RED = (232, 56, 58)
YELLOW = (230, 190, 40)
DARK_PANEL = (22, 22, 35)


class AutoRecorder:
    def __init__(self):
        self._sens_idx = DEFAULT_SENSITIVITY
        self._armed = True
        self._recording = False
        self._manual = False
        self._running = True

        self._rms = 0.0
        self._vu_l = 0.0
        self._vu_r = 0.0
        self._wind = False
        self._graph = [0.0] * 44

        self._wave = None
        self._written = 0
        self._rec_start = None
        self._rec_file = None
        self._silence_at = None
        self._clip_count = 0

        self._free_gb = 0.0
        self._free_gb_ts = 0

        self._pre_buf = collections.deque(maxlen=1000)

        self._out_dir = OUTPUT_DIR
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._clip_count = len(list(self._out_dir.glob("rec-*.wav")))

        self._font_lg = ImageFont.truetype(RobotoMedium, size=52)
        self._font_md = ImageFont.truetype(RobotoMedium, size=36)
        self._font_sm = ImageFont.truetype(RobotoMedium, size=26)

        self._img = Image.new("RGB", (480, 480), BG)
        self._draw = ImageDraw.Draw(self._img)

        self._stream = sounddevice.InputStream(
            device=DEVICE,
            dtype="int16",
            channels=CHANNELS,
            samplerate=SAMPLERATE,
            callback=self._on_audio,
        )
        self._stream.start()

    @property
    def threshold(self):
        return SENSITIVITY_PRESETS[self._sens_idx][1]

    @property
    def sens_name(self):
        return SENSITIVITY_PRESETS[self._sens_idx][0]

    @property
    def running(self):
        return self._running

    # -- audio callback -----------------------------------------------------

    @staticmethod
    def _is_wind(f, samplerate):
        mono = numpy.mean(f, axis=1)
        spectrum = numpy.abs(numpy.fft.rfft(mono))
        freqs = numpy.fft.rfftfreq(len(mono), d=1.0 / samplerate)
        total = numpy.sum(spectrum)
        if total < 1e-12:
            return False
        lf_energy = numpy.sum(spectrum[freqs < WIND_LF_CUTOFF])
        return (lf_energy / total) > WIND_LF_RATIO

    @staticmethod
    def _soft_limit(samples):
        """Soft-knee limiter that compresses peaks approaching ±1.0."""
        t = LIMITER_THRESHOLD
        k = LIMITER_KNEE
        out = numpy.copy(samples)
        abv = numpy.abs(out)
        knee_start = t - k
        mask_knee = (abv > knee_start) & (abv <= t + k)
        mask_over = abv > t + k
        if numpy.any(mask_knee):
            x = abv[mask_knee]
            compressed = knee_start + numpy.tanh((x - knee_start) / k) * k
            out[mask_knee] = numpy.sign(out[mask_knee]) * compressed
        if numpy.any(mask_over):
            x = abv[mask_over]
            compressed = knee_start + numpy.tanh((x - knee_start) / k) * k
            out[mask_over] = numpy.sign(out[mask_over]) * compressed
        return out

    def _on_audio(self, indata, frames, _time, status):
        f = indata.astype(numpy.float64) / 32768.0
        self._rms = float(numpy.sqrt(numpy.mean(f ** 2)))
        self._vu_l = float(numpy.sqrt(numpy.mean(f[:, 0] ** 2)))
        self._vu_r = float(numpy.sqrt(numpy.mean(f[:, 1] ** 2)))

        self._graph.append(min(1.0, self._rms * 12.0))
        self._graph = self._graph[-44:]

        wind = self._rms >= NOISE_GATE and self._is_wind(f, SAMPLERATE)
        self._wind = wind

        if self._rms < NOISE_GATE or wind:
            gated = numpy.zeros_like(indata)
        else:
            limited = self._soft_limit(f)
            gated = (limited * 32768.0).clip(-32768, 32767).astype(numpy.int16)

        self._pre_buf.append(gated.copy())

        if self._recording and self._wave is not None:
            try:
                self._wave.writeframes(gated.tobytes())
                self._written += frames
            except Exception:
                pass

    # -- recording control --------------------------------------------------

    def _start_rec(self):
        if self._recording:
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._rec_file = self._out_dir / f"rec-{ts}.wav"

        self._wave = wave.open(str(self._rec_file), "w")
        self._wave.setframerate(SAMPLERATE)
        self._wave.setsampwidth(2)
        self._wave.setnchannels(CHANNELS)

        target = int(PRE_BUFFER_S * SAMPLERATE)
        chunks = list(self._pre_buf)
        total = 0
        start_idx = len(chunks)
        for i in range(len(chunks) - 1, -1, -1):
            total += len(chunks[i])
            start_idx = i
            if total >= target:
                break
        for c in chunks[start_idx:]:
            self._wave.writeframes(c.tobytes())

        self._written = total
        self._rec_start = time.time()
        self._silence_at = None
        self._recording = True

    def _stop_rec(self, discard=False):
        if not self._recording:
            return
        self._recording = False
        time.sleep(0.02)

        if self._wave is not None:
            self._wave.close()
            self._wave = None

        duration = self._written / SAMPLERATE
        if discard or duration < MIN_RECORDING_S:
            if self._rec_file and self._rec_file.exists():
                self._rec_file.unlink()
        else:
            self._clip_count += 1
            self._enforce_storage()

        self._rec_file = None
        self._written = 0
        self._rec_start = None
        self._silence_at = None
        self._manual = False

    def _rec_duration(self):
        return time.time() - self._rec_start if self._rec_start else 0.0

    def _enforce_storage(self):
        while True:
            total = sum(
                f.stat().st_size for f in self._out_dir.glob("rec-*.wav")
            )
            if total / 1_048_576 <= MAX_STORAGE_MB:
                break
            oldest = sorted(self._out_dir.glob("rec-*.wav"))
            if not oldest:
                break
            oldest[0].unlink()
            self._clip_count = max(0, self._clip_count - 1)

    # -- main-loop tick -----------------------------------------------------

    def update(self):
        now = time.time()
        if now - self._free_gb_ts > 5:
            self._free_gb = shutil.disk_usage(self._out_dir).free / (1024 ** 3)
            self._free_gb_ts = now

        if self._manual:
            if self._rec_duration() > MAX_RECORDING_S:
                self._stop_rec()
            return

        if not self._armed:
            if self._recording:
                self._stop_rec()
            return

        if self._rms > self.threshold and not self._wind:
            self._silence_at = None
            if not self._recording:
                self._start_rec()
        elif self._recording:
            if self._silence_at is None:
                self._silence_at = now
            elif now - self._silence_at > SILENCE_TIMEOUT_S:
                self._stop_rec()

        if self._recording and self._rec_duration() > MAX_RECORDING_S:
            self._stop_rec()

    # -- button handlers ----------------------------------------------------

    def toggle_arm(self):
        self._armed = not self._armed

    def cycle_sens(self):
        self._sens_idx = (self._sens_idx + 1) % len(SENSITIVITY_PRESETS)

    def toggle_manual(self):
        if self._recording:
            self._stop_rec()
        else:
            self._manual = True
            self._start_rec()

    def shutdown(self):
        self._stop_rec()
        self._stream.stop()
        self._running = False

    # -- display ------------------------------------------------------------

    def render(self):
        d = self._draw
        d.rectangle((0, 0, 480, 480), fill=BG)

        # status indicator
        if self._recording:
            dot, label, lc = RED, "REC", RED
        elif self._armed:
            dot, label, lc = GREEN, "ARMED", GREEN
        else:
            dot, label, lc = DIM, "IDLE", DIM

        d.ellipse((20, 16, 60, 56), fill=dot)
        d.text((72, 12), label, fill=lc, font=self._font_lg)

        if self._recording:
            dur = self._rec_duration()
            d.text(
                (320, 12),
                f"{int(dur // 60):02d}:{int(dur % 60):02d}",
                fill=RED,
                font=self._font_lg,
            )

        # sensitivity
        d.text((20, 75), f"Sens: {self.sens_name}", fill=WHITE, font=self._font_sm)
        d.text(
            (220, 75),
            f"Thresh: {self.threshold:.3f}",
            fill=DIM,
            font=self._font_sm,
        )

        # level meter
        mx, my, mw, mh = 20, 115, 440, 35
        d.rectangle((mx, my, mx + mw, my + mh), fill=DARK_PANEL)

        level = min(1.0, self._rms / 0.12)
        fw = int(mw * level)
        if fw > 0:
            c = RED if self._rms > self.threshold else GREEN
            d.rectangle((mx, my, mx + fw, my + mh), fill=c)

        tx = mx + int(mw * min(1.0, self.threshold / 0.12))
        d.line([(tx, my - 5), (tx, my + mh + 5)], fill=YELLOW, width=3)

        # waveform
        wy, wh = 170, 150
        d.rectangle((20, wy, 460, wy + wh), fill=DARK_PANEL)
        wmid = wy + wh // 2
        bx = 20
        for val in self._graph:
            bh = max(2, int(wh * 0.85 * val))
            top = wmid - bh // 2
            bot = wmid + bh // 2
            if self._recording:
                c = RED
            elif self._armed and val > 0.15:
                c = GREEN
            else:
                c = DIM
            d.rectangle((bx, top, bx + 5, bot), fill=c)
            bx += 10

        # VU meters
        vy = 340
        d.text((20, vy), "L", fill=WHITE, font=self._font_sm)
        d.text((20, vy + 32), "R", fill=WHITE, font=self._font_sm)
        for i, vu in enumerate([self._vu_l, self._vu_r]):
            y = vy + i * 32
            d.rectangle((55, y + 4, 460, y + 26), fill=DARK_PANEL)
            vw = int(405 * min(1.0, vu / 0.08))
            if vw > 0:
                d.rectangle((55, y + 4, 55 + vw, y + 26), fill=GREEN)

        # stats
        d.text((20, 415), f"Clips: {self._clip_count}", fill=WHITE, font=self._font_sm)
        d.text(
            (240, 415),
            f"Free: {self._free_gb:.1f} GB",
            fill=WHITE,
            font=self._font_sm,
        )

        # button hints
        for txt, x in [("A:Arm", 20), ("B:Sens", 140), ("X:Rec", 280), ("Y:Off", 390)]:
            d.text((x, 455), txt, fill=DIM, font=self._font_sm)

        return self._img


def main():
    display = ST7789(
        rotation=90,
        port=0,
        cs=1,
        dc=9,
        backlight=13,
        spi_speed_hz=80_000_000,
    )

    rec = AutoRecorder()

    handlers = {
        5: rec.toggle_arm,
        6: rec.cycle_sens,
        16: rec.toggle_manual,
        24: rec.shutdown,
    }

    gpio_handle = lgpio.gpiochip_open(0)
    for pin in BUTTONS:
        lgpio.gpio_claim_input(gpio_handle, pin, lgpio.SET_PULL_UP)

    debounce = {pin: 0.0 for pin in BUTTONS}
    DEBOUNCE_S = 0.3

    def poll_buttons():
        now = time.time()
        for pin in BUTTONS:
            if lgpio.gpio_read(gpio_handle, pin) == 0 and now - debounce[pin] > DEBOUNCE_S:
                debounce[pin] = now
                handlers.get(pin, lambda: None)()

    for sig_num in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig_num, lambda *_: rec.shutdown())

    try:
        while rec.running:
            poll_buttons()
            rec.update()
            display.display(rec.render().resize((240, 240)))
            time.sleep(1.0 / DISPLAY_FPS)
    finally:
        lgpio.gpiochip_close(gpio_handle)


if __name__ == "__main__":
    main()
