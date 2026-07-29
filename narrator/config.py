"""Configuration. Everything tunable lives in config.toml; this module loads
and validates it.

Nothing in this package reads a magic number from source. If you find one,
it is a bug -- move it here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AppConfig(BaseModel):
    log_db: str = "logs/narrator.sqlite"
    log_file: str = "logs/narrator.log"
    log_level: str = "INFO"


class MarketConfig(BaseModel):
    symbol: str = ""
    symbol_candidates: list[str] = Field(
        default_factory=lambda: ["XAUUSD", "GOLD", "XAU/USD"]
    )
    tick_poll_ms: int = 250
    bar_poll_ms: int = 5000
    bars_history: int = 500
    timeframes: list[str] = Field(
        default_factory=lambda: ["M1", "M5", "M15", "H1", "H4", "D1"]
    )
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    stale_tick_seconds: float = 120.0
    # The freshness contract. A price older than this is withheld from the
    # fact set entirely, so no line -- library or host -- can quote it.
    max_quote_age_seconds: float = 15.0

    @field_validator("timeframes")
    @classmethod
    def _known_timeframes(cls, v: list[str]) -> list[str]:
        known = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"}
        bad = [tf for tf in v if tf not in known]
        if bad:
            raise ValueError(f"unknown timeframes {bad}; known: {sorted(known)}")
        return v


class ReplayConfig(BaseModel):
    csv: str = "tests/fixtures/xauusd_m1.csv"
    speed: float = 60.0
    start_at: str = ""
    loop: bool = False
    spread: float = 0.28  # synthetic ask-bid, the csv has no tick data
    max_virtual_step: float = 1.0  # seconds the virtual clock may jump at once


class SessionsConfig(BaseModel):
    sydney: tuple[int, int] = (21, 6)
    tokyo: tuple[int, int] = (0, 9)
    london: tuple[int, int] = (7, 16)
    newyork: tuple[int, int] = (12, 21)
    weekend_close_day: int = 4
    weekend_close_hour: int = 21
    weekend_open_day: int = 6
    weekend_open_hour: int = 21


class FactsConfig(BaseModel):
    atr_period: int = 14
    flat_threshold: float = 0.5
    stuck_atr_fraction: float = 0.3
    expansion_ratio: float = 1.3
    contraction_ratio: float = 0.7
    tight_range_atr: float = 0.75
    level_test_tolerance: float = 0.10
    asian_average_days: int = 20


class SchedulerConfig(BaseModel):
    tick_seconds: float = 2.0
    min_gap_seconds: float = 12.0
    target_density: float = 0.35
    density_window_seconds: float = 600.0
    bridge_after_seconds: float = 90.0
    default_cooldown: int = 300
    default_max_per_session: int = 20
    recent_memory: int = 12
    recency_penalty: float = 0.15
    reset_on_session_change: bool = True
    session_reset_hours: float = 6.0  # backstop for long single sessions


class TemplatesConfig(BaseModel):
    dir: str = "templates"
    hot_reload: bool = True
    reload_poll_seconds: float = 2.0


class SpeechConfig(BaseModel):
    voice: str = "am_michael"
    speed: float = 1.0
    lang_code: str = "a"
    sample_rate: int = 24000
    cache_dir: str = "cache/phrases"
    cache_enabled: bool = True
    words_per_second: float = 2.7
    min_utterance_seconds: float = 1.2


class AudioConfig(BaseModel):
    device: str = ""
    volume: float = 1.0


class PersonaConfig(BaseModel):
    """One of the two hosts. `brief` is what stops them sounding identical."""

    key: str
    name: str
    voice: str
    brief: str = ""
    avatar: str = ""          # roster filename; blank = whatever is on screen


class HostsConfig(BaseModel):
    """The two-host conversation layer.

    Off by default and off without a key, in which case the template library
    runs the stream exactly as it did before this existed.

    The API key is deliberately NOT read from this file. It comes from the
    ANTHROPIC_API_KEY environment variable, because config.toml is the file
    most likely to be pasted into a chat, screenshotted on stream, or committed
    by accident -- and this project's whole output is a public broadcast.
    """

    enabled: bool = False
    # "ollama" runs a model on this machine, free and unmetered. "anthropic"
    # uses the hosted API and needs ANTHROPIC_API_KEY. "auto" prefers local.
    backend: str = "ollama"
    model: str = "qwen2.5:7b-instruct-q4_K_M"
    ollama_host: str = "http://127.0.0.1:11434"
    max_tokens: int = 120
    temperature: float = 1.0
    memory_turns: int = 14
    queue_depth: int = 6
    timeout_seconds: float = 25.0
    # One turn in this many arrives with something to bring up -- see
    # narrator/script/topics.py. 0 turns it off.
    topic_every: int = 4
    # A short sound in the incoming host's voice on a change of speaker, played
    # while their line is still being synthesised. See speech/fillers.py.
    turn_taking_sounds: bool = True
    # Silence before a written reply lands, in seconds. People come back at
    # each other in about a second; the scheduler's eight-second floor between
    # market calls is far too long to sound like a conversation.
    reply_gap_seconds: float = 1.2
    # Fraction of wall-clock spent speaking while podcast mode is on. The
    # solo narrator's target is a third; two people in conversation talk for
    # most of the hour, and holding them to a narrator's budget is what puts
    # half a minute between a question and its answer.
    podcast_density: float = 0.7
    # Fraction of speaking slots the hosts take. The library keeps the rest,
    # because a level breaking or a fill landing should be said by the thing
    # that knows it happened the instant it happened.
    share: float = 0.6
    # Never let the conversation take a slot that a high-priority market event
    # wanted. Library templates at or above this priority always win.
    yield_to_priority: int = 3
    personas: list[PersonaConfig] = Field(default_factory=list)


class CommunityConfig(BaseModel):
    """Where the audience is being sent, and how hard.

    Kept out of the template text so the name, the platform and the phrasing
    can change without editing thirty strings -- and so the call to action can
    be turned off entirely for a stream that is not promoting anything.

    `every_minutes` is the floor between plugs. The templates carry their own
    cooldowns as well; this is the one that stops the whole *category* turning
    a market stream into an advert, which is the way these go wrong.
    """

    enabled: bool = True
    name: str = "TradeFix"
    platform: str = "Telegram"
    where: str = "link in the bio"
    every_minutes: float = 12.0


class AvatarEntry(BaseModel):
    """One character offered in the browser UI's avatar picker.

    `focus_height` is what the framing camera orbits -- roughly the face, in
    metres. Models differ enormously in height (a chibi's face sits near 0.9,
    an adult VRM's near 1.5), so switching avatar without re-framing leaves
    the camera pointing at an empty room. For `.vrm` files this is measured
    from the model's head bone when it is left at 0; `.warudo` characters are
    opaque, so those need the number here.
    """

    file: str
    label: str = ""
    gender: str = "other"
    focus_height: float = 0.0  # 0 = measure it from the file

    # The character's setting: where they stand in the room, and the shot the
    # camera takes of them. This is what makes one avatar a close-up talking
    # head and another a wide shot of someone at their desk.
    setting: str = ""  # free text, shown in the picker
    x: float = 0.0  # metres, + is to the character's left as the camera sees it
    y: float = 0.0  # metres; negative drops them to seated height
    z: float = 0.0  # metres, + is towards the camera
    facing: float = 0.0  # degrees the character is turned
    yaw: float = 0.0  # camera angle around them
    pitch: float = 6.0  # camera elevation
    distance: float = 0.85  # how far the camera sits back
    # Slides the camera sideways without turning it, so the subject sits off
    # centre and the room fills the rest of the frame. This is the difference
    # between a passport photo and a stream layout: negative puts them on the
    # left third, positive on the right.
    offset: float = 0.0

    @property
    def source(self) -> str:
        return f"character://data/Characters/{self.file}"

    @property
    def name(self) -> str:
        return self.label or self.file.rsplit(".", 1)[0].replace("_", " ")


class WarudoConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 19190
    path: str = "/"
    viseme_fps: int = 60
    emote_debounce_seconds: float = 60.0
    reconnect_seconds: float = 3.0
    # Warudo's WebSocket server accepts exactly one envelope:
    #     {"action": "<name>", "data": <value>}
    # Anything without an "action" is discarded with "Received data but
    # action is null". Each viseme is therefore its own action, matched by an
    # "On WebSocket Action" node in the blueprint.
    action_prefix: str = "viseme_"
    emote_action: str = "emote"
    # Which name to send for an emote: "vrm0" (Joy/Sorrow/Fun -- what the
    # installed CC0 avatars use), "vrm1" (happy/relaxed/surprised), or
    # "name" for the narrator's own (excited/bored/alert).
    expression_style: str = "vrm0"
    # Skip a channel whose weight has not meaningfully moved. At 60fps this
    # is the difference between 300 messages a second and near-silence while
    # the mouth is closed.
    viseme_epsilon: float = 0.01
    # Response curve for the avatar's morphs, applied as weight ** gamma.
    #
    # A blendshape's *visible* travel is not linear in its weight, and models
    # differ enormously: a well-built mouth reads clearly at 0.4, while the
    # stylised CC0 avatars barely move below 0.7. The viseme engine deals in
    # articulation -- an unstressed "ee" genuinely is a half-open mouth -- so
    # the correction belongs here, at the point where weights meet a specific
    # avatar, not in the phonetics.
    #
    # 1.0 leaves weights alone. Below 1.0 lifts the quiet end while leaving
    # 1.0 at 1.0, so the ordering between vowels survives and only the
    # visibility changes. Zero stays zero, so lip closures stay absolute.
    viseme_gamma: float = 1.0
    # The framing control on the avatar panel drives these two actions, each
    # carrying a Vector3, into Set Asset Position / Set Asset Rotation on the
    # camera. Framing a VTuber by typing transform numbers is miserable, and
    # doing it in Warudo's window means alt-tabbing away from the thing you
    # are trying to frame.
    camera_position_action: str = "cam_pos"
    camera_rotation_action: str = "cam_rot"
    # The avatar picker drives this one into Set Asset Property -> Source.
    avatar_action: str = "avatar"
    # Reloading the scene is what makes an avatar switch land: Warudo unloads
    # the old character on a Source change without loading the replacement.
    reload_action: str = "reload"
    scene_name: str = "DefaultScene"
    # What the camera orbits: roughly the character's face, in metres. Chibi
    # models sit near 1.0, adult-proportioned ones near 1.5.
    camera_focus_height: float = 1.35
    # The avatar picker's roster. Empty means the picker offers nothing --
    # the narrator does not go rummaging in Warudo's install on its own,
    # because which characters are licensed for a commercial stream is the
    # operator's call, not something to infer from a folder listing.
    avatars: list[AvatarEntry] = Field(default_factory=list)
    # narrator emote -> [VRM 1.0 preset, VRM 0.x preset]. Mapping onto the
    # standard presets means any stock VRM works without the operator
    # authoring custom expression clips first.
    expressions: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "neutral": ["neutral", "Neutral"],
            "alert": ["surprised", "Fun"],
            "surprised": ["surprised", "Fun"],
            "bored": ["relaxed", "Sorrow"],
            "excited": ["happy", "Joy"],
        }
    )


class UIConfig(BaseModel):
    silence_marker_seconds: float = 30.0
    status_every_seconds: float = 900.0  # 0 = never
    fact_panel_keys: list[str] = Field(
        default_factory=lambda: [
            "price",
            "change_day",
            "session",
            "minutes_to_next_session",
            "atr_m15",
            "atr_ratio",
            "minutes_since_move",
            "nearest_level",
            "nearest_level_dist",
        ]
    )


class WebUIConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8770  # websocket runs on port + 1
    open_browser: bool = True
    # Mirror Warudo's render window into the dashboard, so the avatar and the
    # numbers are one view instead of two windows.
    avatar_capture: bool = True
    avatar_window: str = "Warudo"
    avatar_fps: int = 15
    avatar_width: int = 640
    avatar_quality: int = 62


class PreflightConfig(BaseModel):
    require_cuda: bool = True
    required_capability: tuple[int, int] = (12, 0)
    require_mt5: bool = True
    require_warudo: bool = False
    # Refuse to start on a feed that is not real time. --allow-delayed is the
    # only way past it, and it says so on the transcript for the whole run.
    require_realtime: bool = True


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    facts: FactsConfig = Field(default_factory=FactsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    templates: TemplatesConfig = Field(default_factory=TemplatesConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    community: CommunityConfig = Field(default_factory=CommunityConfig)
    hosts: HostsConfig = Field(default_factory=HostsConfig)
    warudo: WarudoConfig = Field(default_factory=WarudoConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)

    # Filled in by load_config(); not part of the toml.
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)


def load_config(path: str | Path | None = None) -> Config:
    """Load config.toml. Missing file -> all defaults (useful for tests)."""
    if path is None:
        path = project_root() / "config.toml"
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    cfg = Config(**raw)
    cfg.root = path.parent.resolve() if path.exists() else project_root()
    return cfg


def project_root() -> Path:
    """The tradefix-narrator/ directory (parent of the narrator package)."""
    return Path(__file__).resolve().parent.parent
