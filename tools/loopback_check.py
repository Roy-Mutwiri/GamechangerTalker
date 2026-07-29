"""Can Warudo hear the narrator?

    python -m tools.loopback_check

Warudo's Lip Sync animates the mouth from an audio *input* device. The
narrator plays to an audio *output* device. The bridge between them is a
loopback input -- on this hardware, Realtek's "Stereo Mix" -- which presents
whatever the speakers are playing as a recordable source.

This records from each candidate loopback device and reports the signal
level, so you know before configuring Warudo whether it will hear anything.
Run it while the narrator is speaking, or pass --tone and it plays its own.
"""

from __future__ import annotations

import argparse
import math
import threading
import time

TONE_HZ = 440.0
TONE_LEVEL = 0.3


def _play_tone(stop: threading.Event) -> None:
    """Half-second bursts of a sine on the default output until told to stop."""
    import numpy as np
    import sounddevice as sd

    rate = 48000
    t = np.arange(int(rate * 0.5)) / rate
    tone = (TONE_LEVEL * np.sin(2 * math.pi * TONE_HZ * t)).astype(np.float32)
    stereo = np.column_stack([tone, tone])
    while not stop.is_set():
        sd.play(stereo, rate, blocking=True)


def _measure(index: int, seconds: float) -> tuple[float, float, int]:
    """Peak, mean RMS and block count over `seconds` of capture.

    Uses a callback stream rather than blocking reads: WDM-KS devices -- which
    is how Stereo Mix shows up on this machine -- reject the blocking API with
    "Blocking API not supported yet" even when they are working perfectly.
    """
    import numpy as np
    import sounddevice as sd

    info = sd.query_devices(index)
    rate = int(info["default_samplerate"])
    channels = min(2, int(info["max_input_channels"]))
    peak = 0.0
    level_sum = 0.0
    blocks = 0

    def on_block(indata, frames, timeinfo, status):
        nonlocal peak, level_sum, blocks
        magnitude = np.abs(indata)
        if magnitude.size:
            peak = max(peak, float(magnitude.max()))
            level_sum += float(np.sqrt((indata.astype(float) ** 2).mean()))
            blocks += 1

    with sd.InputStream(
        device=index,
        channels=channels,
        samplerate=rate,
        dtype="float32",
        callback=on_block,
    ):
        time.sleep(seconds)

    return peak, level_sum / max(1, blocks), blocks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument(
        "--tone",
        action="store_true",
        help="play a 440Hz tone on the default output while measuring, "
        "instead of needing the narrator to be speaking",
    )
    args = ap.parse_args()

    import sounddevice as sd

    devices = sd.query_devices()
    apis = {i: api["name"] for i, api in enumerate(sd.query_hostapis())}
    loopbacks = [
        (index, info["name"], apis[info["hostapi"]])
        for index, info in enumerate(devices)
        if info["max_input_channels"] > 0
        and any(
            word in info["name"].lower()
            for word in ("stereo mix", "loopback", "what u hear", "wave out")
        )
    ]
    if args.device is not None:
        info = devices[args.device]
        loopbacks = [(args.device, info["name"], apis[info["hostapi"]])]
    if not loopbacks:
        print("No loopback input found.")
        print("Enable it: Windows Sound settings -> Recording -> right click ->")
        print('"Show Disabled Devices" -> enable "Stereo Mix".')
        print("\nWithout one, Warudo cannot hear the narrator and its Lip Sync")
        print("will sit silent. The alternative is the phoneme bridge blueprint.")
        return

    stop = threading.Event()
    if args.tone:
        print(f"output: {sd.query_devices(kind='output')['name']}")
        threading.Thread(target=_play_tone, args=(stop,), daemon=True).start()
        time.sleep(0.5)
        print(f"playing a {TONE_HZ:g}Hz tone; {args.seconds:g}s per device\n")
    else:
        print(f"listening for {args.seconds:g}s per device -- play some audio now\n")

    heard: list[tuple[str, str]] = []
    try:
        for index, name, api in loopbacks:
            print(f"  [{index}] {name[:44]:<44} ({api})")
            try:
                peak, level, blocks = _measure(index, args.seconds)
            except Exception as exc:
                print(f"       could not open: {exc}")
                continue
            db = 20 * math.log10(level) if level > 1e-9 else -120.0
            if peak > 0.01:
                verdict = "HEARS AUDIO"
                heard.append((name, api))
            else:
                verdict = "silent -- nothing reaching it"
            print(f"       peak {peak:.4f}   rms {level:.5f} ({db:.0f} dBFS)   {verdict}")
            if not blocks:
                print("       (no blocks captured -- the stream opened but never fired)")
    finally:
        stop.set()
        sd.stop()

    print()
    if heard and any(api != "Windows WDM-KS" for _, api in heard):
        print("Select that device as the Microphone in Warudo's Lip Sync settings")
        print("and the mouth will follow the narrator.")
    elif heard:
        print("The signal is there, but only on the WDM-KS pin -- the raw driver")
        print("input. Warudo is Unity, and Unity offers only Windows *endpoints*.")
        print("An endpoint that no host API but WDM-KS lists is disabled, so Warudo")
        print("will not show it in the Lip Sync microphone dropdown.")
        print()
        print("Enable it:  control mmsys.cpl,,1   ->  Recording tab  ->  right click")
        print('  ->  "Show Disabled Devices"  ->  right click Stereo Mix  ->  Enable')
        print("Then run this again: it should be listed under MME/DirectSound/WASAPI.")
    else:
        print("Nothing reached any loopback device. Check that the narrator (or the")
        print("tone, with --tone) is playing to the same output Stereo Mix taps --")
        print(f"currently {sd.query_devices(kind='output')['name']}.")


if __name__ == "__main__":
    main()
