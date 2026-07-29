"""Boot-time environment checks.

Fail loudly here, not mysteriously mid-stream.

The RTX 5080 is Blackwell (sm_120) and needs CUDA 12.8 kernels. A default
PyPI torch wheel imports fine, reports cuda.is_available() == True, and then
dies on the first kernel launch with:

    RuntimeError: CUDA error: no kernel image is available for execution
                  on the device

That failure surfaces inside Kokoro, an hour into a live stream. So we check
the device capability at boot and refuse to start.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field

from narrator.config import Config

log = logging.getLogger(__name__)

CUDA_128_HINT = (
    "Install the CUDA 12.8 build of torch:\n"
    "    pip uninstall -y torch torchaudio\n"
    "    pip install torch torchaudio "
    "--index-url https://download.pytorch.org/whl/cu128"
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fatal: bool = False


@dataclass
class PreflightReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.fatal]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and not r.fatal]

    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        lines = []
        for r in self.results:
            mark = "OK  " if r.ok else ("FAIL" if r.fatal else "WARN")
            lines.append(f"  [{mark}] {r.name}: {r.detail}")
        return "\n".join(lines)


def check_cuda(cfg: Config) -> CheckResult:
    """Verify torch sees a device with the required compute capability."""
    want = tuple(cfg.preflight.required_capability)
    try:
        import torch
    except ImportError:
        return CheckResult(
            "cuda",
            False,
            f"torch is not installed. {CUDA_128_HINT}",
            fatal=True,
        )

    if not torch.cuda.is_available():
        return CheckResult(
            "cuda",
            False,
            "torch.cuda.is_available() is False -- no usable CUDA device. "
            f"torch {torch.__version__}, built for CUDA {torch.version.cuda}. "
            f"{CUDA_128_HINT}",
            fatal=True,
        )

    cap = torch.cuda.get_device_capability()
    name = torch.cuda.get_device_name(0)
    detail = (
        f"{name}, compute capability {cap[0]}.{cap[1]}, "
        f"torch {torch.__version__} (CUDA {torch.version.cuda})"
    )
    if tuple(cap) != want:
        return CheckResult(
            "cuda",
            False,
            f"{detail} -- expected sm_{want[0]}{want[1]}. "
            f"If this really is the intended GPU, change "
            f"preflight.required_capability in config.toml. {CUDA_128_HINT}",
            fatal=True,
        )

    # is_available() lying is the failure mode we actually care about, so
    # launch one real kernel.
    try:
        t = torch.zeros(8, device="cuda")
        _ = (t + 1).sum().item()
    except Exception as exc:  # pragma: no cover - hardware dependent
        return CheckResult(
            "cuda",
            False,
            f"{detail} -- test kernel failed: {exc}. {CUDA_128_HINT}",
            fatal=True,
        )

    return CheckResult("cuda", True, detail)


def check_mt5(cfg: Config) -> CheckResult:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return CheckResult(
            "mt5",
            False,
            "MetaTrader5 package not installed (pip install MetaTrader5). "
            "Use --replay to run without it.",
            fatal=True,
        )
    if not mt5.initialize():
        return CheckResult(
            "mt5",
            False,
            f"mt5.initialize() failed: {mt5.last_error()}. Is the MetaTrader 5 "
            "terminal running and logged in? Use --replay to run without it.",
            fatal=True,
        )
    info = mt5.terminal_info()
    acct = mt5.account_info()
    who = f"{acct.server} #{acct.login}" if acct else "no account"
    return CheckResult(
        "mt5", True, f"connected to {info.name if info else 'terminal'} ({who})"
    )


def check_warudo(cfg: Config) -> CheckResult:
    """A dead avatar bridge is a warning, never fatal. Audio is the stream."""
    host, port = cfg.warudo.host, cfg.warudo.port
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return CheckResult("warudo", True, f"websocket reachable at {host}:{port}")
    except OSError as exc:
        return CheckResult(
            "warudo",
            False,
            f"nothing listening on {host}:{port} ({exc.__class__.__name__}). "
            "Check the WebSocket port in Warudo and set warudo.port in "
            "config.toml -- see WARUDO_SETUP.md. The narrator will keep "
            "speaking without the avatar.",
            fatal=cfg.preflight.require_warudo,
        )


def check_prices(cfg: Config, source: str, realtime: bool, allowed: bool) -> CheckResult:
    """Refuse to narrate prices the feed cannot vouch for.

    A wrong avatar is visible, a wrong voice is audible, and a wrong price is
    neither -- it sounds exactly like a right one. This is the check that
    stops a recorded file or a ten-minute-delayed quote reaching the stream
    because nobody remembered which flag was passed an hour ago.
    """
    if realtime:
        return CheckResult("prices", True, f"real time from {source}")
    if allowed:
        return CheckResult(
            "prices",
            False,
            f"NOT REAL TIME -- {source}, allowed by --allow-delayed. "
            "Every price this run is stale; do not put it on a stream.",
            fatal=False,
        )
    return CheckResult(
        "prices",
        False,
        f"{source} is not a real-time feed, and preflight.require_realtime is "
        "on. Attach MetaTrader 5 (terminal running and logged in, a free demo "
        "account is enough) for broker ticks, or pass --allow-delayed to run "
        "on stale prices deliberately.",
        fatal=True,
    )


def check_templates(cfg: Config) -> CheckResult:
    from narrator.script.library import TemplateLibrary

    path = cfg.path(cfg.templates.dir)
    try:
        lib = TemplateLibrary(path, cfg)
        lib.load()
    except Exception as exc:
        return CheckResult("templates", False, str(exc), fatal=True)
    return CheckResult(
        "templates",
        True,
        f"{len(lib.templates)} templates from {len(lib.files)} files in {path}",
    )


def run_preflight(
    cfg: Config,
    *,
    need_cuda: bool,
    need_mt5: bool,
    need_warudo: bool,
    price_source: str = "",
    prices_realtime: bool = True,
    allow_delayed: bool = False,
) -> PreflightReport:
    report = PreflightReport()

    if price_source and cfg.preflight.require_realtime:
        report.results.append(
            check_prices(cfg, price_source, prices_realtime, allow_delayed)
        )

    if need_cuda and cfg.preflight.require_cuda:
        report.results.append(check_cuda(cfg))
    else:
        report.results.append(
            CheckResult("cuda", True, "skipped (no speech engine this run)")
        )

    if need_mt5 and cfg.preflight.require_mt5:
        report.results.append(check_mt5(cfg))
    else:
        report.results.append(CheckResult("mt5", True, "skipped (replay adapter)"))

    if need_warudo:
        report.results.append(check_warudo(cfg))
    else:
        report.results.append(CheckResult("warudo", True, "skipped (no avatar this run)"))

    report.results.append(check_templates(cfg))
    return report
