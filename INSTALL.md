# Installing Gamechanger Talker

One installer, one time. Download it, run it, and it fetches everything the
narrator needs on its own.

**[Download the latest installer](https://github.com/Roy-Mutwiri/GamechangerTalker/releases/latest)**
-- `GamechangerTalkerSetup.exe`

---

## What you need

| | |
|---|---|
| Windows | 10 or 11, 64-bit |
| Graphics | NVIDIA card strongly recommended. It runs without one, much slower. |
| Disk | About 20 GB free |
| Internet | The first install downloads ~15 GB |
| Broker account | Optional. A free MetaTrader 5 demo is enough, and the installer sets the terminal up for you. |

No administrator rights needed. It installs under your own user account.

## Installing

1. Run `GamechangerTalkerSetup.exe`.
2. Leave **"Download and install requirements now"** ticked.
3. Wait. A console window shows what it is doing, step by step.

That console is doing real work and can sit on one line for a long time -- the
AI models are 9 GB and 3 GB. It is not stuck. On a normal home connection the
whole thing takes 30-60 minutes.

If Windows shows a blue "Windows protected your PC" box, that is SmartScreen
reacting to an installer it has not seen before, not a virus warning. Click
**More info**, then **Run anyway**.

### What it installs

| | |
|---|---|
| Python 3.12 | Only if you do not have it. Not 3.13 -- the speech engine refuses to install there. |
| PyTorch | CUDA 12.8 build if you have an NVIDIA card, CPU build otherwise |
| Kokoro | The voice. Downloads its model on first speech. |
| Ollama + 2 models | The two AI hosts, and chart reading |
| MetaTrader 5 | The live price feed |

Nothing is bundled into the exe itself. It downloads current versions at
install time, so the installer does not go stale.

## First run

1. **Open MetaTrader 5 and log in.** A free demo account is fine -- the terminal
   offers to open one when it first starts. Leave it running; the narrator reads
   prices out of it.
2. Start **Gamechanger Talker** from the Start Menu or your desktop.
3. A browser view opens at `http://127.0.0.1:8770` with the dashboard, the
   transcript, the voice picker and the avatar.

Anything you type in the console is spoken next. `/quit` stops it cleanly.

### Trying it without a broker account

Use **Gamechanger Talker (replay, no MetaTrader needed)** in the Start Menu. It
runs on recorded bars with real speech, so you can hear it before setting
anything up. The prices are not live and it says so on screen.

## If something goes wrong

**"Setup did not complete"** -- run **Gamechanger Talker Setup** from the Start
Menu. It picks up where it stopped; finished steps are skipped.

**The hosts never say anything** -- Ollama is missing or its models did not
download. Re-run setup. The stream keeps working off its template library in the
meantime, so this is not fatal.

**"refusing to start -- prices"** -- MetaTrader 5 is not running, or is running
but not logged in. Open it, log in, leave it open.

**It speaks but there is no sound** -- Windows is sending it to the wrong output
device. Run this from the install folder to see what it can find, then set
`audio.device` in `config.toml`:

```powershell
.venv\Scripts\python.exe -m narrator.main --list-devices
```

**Redo everything from scratch:**

```powershell
powershell -ExecutionPolicy Bypass -File installer\setup.ps1 -Force
```

## Uninstalling

Add or Remove Programs, or the Start Menu shortcut. It removes the virtual
environment and cache too.

Two things it leaves alone deliberately, because you may want them: MetaTrader 5
and Ollama. Uninstall those separately. The AI models live in
`%USERPROFILE%\.ollama` and are worth deleting by hand if you want the 12 GB
back.

---

## Building the installer yourself

```powershell
winget install --id JRSoftware.InnoSetup
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Output is `dist\GamechangerTalkerSetup.exe`.

| File | Does what |
|---|---|
| `installer/GamechangerTalker.iss` | What goes in the exe, the shortcuts, the uninstaller |
| `installer/setup.ps1` | Installs the dependencies. Idempotent -- safe to re-run. |
| `installer/launch.ps1` | What the shortcuts point at. Finishes setup if needed, starts MetaTrader if it is closed. |
| `installer/build.ps1` | Builds the exe |

`setup.ps1` reads the model names out of `config.toml` rather than hardcoding
them, so changing the model in config does not leave every new machine pulling
the old one.
