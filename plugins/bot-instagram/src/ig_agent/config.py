"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
FILTERED_DIR = DATA_DIR / "filtered"
MEDIA_DIR = DATA_DIR / "media"
REPORTS_DIR = PROJECT_ROOT / "reports"
AGENCY_CONTEXT_PATH = PROJECT_ROOT / "agency_context.json"
DB_PATH = DATA_DIR / "interactions.db"
ANALYZER_DB_PATH = DATA_DIR / "video_analyses.db"
ANALYZER_UPLOAD_DIR = MEDIA_DIR / "uploads"

_DEFAULT_CHROME_PATHS = {
    "win32": "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "linux": "/usr/bin/google-chrome",
}


def _default_chrome_path() -> str:
    return _DEFAULT_CHROME_PATHS.get(sys.platform, _DEFAULT_CHROME_PATHS["linux"])


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    moonshot_api_key: str = field(default_factory=lambda: os.getenv("MOONSHOT_API_KEY", ""))
    kimi_base_url: str = field(
        default_factory=lambda: os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    )
    kimi_filter_model: str = field(
        default_factory=lambda: os.getenv("KIMI_FILTER_MODEL", "kimi-k2.6")
    )
    kimi_synth_model: str = field(
        default_factory=lambda: os.getenv("KIMI_SYNTH_MODEL", "kimi-k3")
    )
    kimi_browser_model: str = field(
        default_factory=lambda: os.getenv("KIMI_BROWSER_MODEL", "kimi-k2.6")
    )
    kimi_request_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("KIMI_REQUEST_TIMEOUT_S", "60"))
    )
    # Synthesis embeds the full filtered shortlist + multimodal notes and asks
    # for a multi-section markdown report — a much heavier call than filter/
    # draft scoring, so it gets its own, longer timeout.
    kimi_synth_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("KIMI_SYNTH_TIMEOUT_S", "120"))
    )
    # Video/image analysis (multimodal + Analyzer) also needs more time than a
    # plain text call — uploading + processing media isn't instant.
    kimi_multimodal_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("KIMI_MULTIMODAL_TIMEOUT_S", "90"))
    )
    filter_batch_size: int = field(
        default_factory=lambda: int(os.getenv("FILTER_BATCH_SIZE", "6"))
    )
    chrome_path: str = field(
        default_factory=lambda: os.getenv("CHROME_PATH", _default_chrome_path())
    )
    browser_user_data_dir: Path = field(
        default_factory=lambda: _expand_path(
            # Avoid "chrome" in the path — browser-use treats those as system
            # Chrome profiles and copies them to a disposable temp directory.
            os.getenv("BROWSER_USER_DATA_DIR", "./data/browser-profile")
        )
    )
    max_posts_per_session: int = field(
        default_factory=lambda: int(os.getenv("MAX_POSTS_PER_SESSION", "5"))
    )
    max_scroll_sessions_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_SCROLL_SESSIONS_PER_DAY", "10"))
    )
    session_max_minutes: int = field(
        default_factory=lambda: int(os.getenv("SESSION_MAX_MINUTES", "12"))
    )
    multimodal_top_n: int = field(
        default_factory=lambda: int(os.getenv("MULTIMODAL_TOP_N", "3"))
    )
    enable_multimodal: bool = field(
        default_factory=lambda: _env_bool("ENABLE_MULTIMODAL", False)
    )
    relevance_threshold: int = field(
        default_factory=lambda: int(os.getenv("RELEVANCE_THRESHOLD", "35"))
    )

    # Engagement daily caps
    max_likes_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_LIKES_PER_DAY", "80"))
    )
    max_follows_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_FOLLOWS_PER_DAY", "40"))
    )
    max_comments_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_COMMENTS_PER_DAY", "12"))
    )
    max_dms_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_DMS_PER_DAY", "8"))
    )
    max_posts_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_POSTS_PER_DAY", "2"))
    )
    engage_after_research: bool = field(
        default_factory=lambda: _env_bool("ENGAGE_AFTER_RESEARCH", True)
    )
    # Soft pause after IG challenge/throttle (minutes). Escalates up to max on repeats.
    engage_circuit_minutes: int = field(
        default_factory=lambda: int(os.getenv("ENGAGE_CIRCUIT_MINUTES", "5"))
    )
    engage_circuit_max_minutes: int = field(
        default_factory=lambda: int(os.getenv("ENGAGE_CIRCUIT_MAX_MINUTES", "20"))
    )
    # Extra browser profiles to rotate into after a throttle (same login may still be needed)
    engage_profile_slots: int = field(
        default_factory=lambda: int(os.getenv("ENGAGE_PROFILE_SLOTS", "2"))
    )
    use_scripted_engagement: bool = field(
        default_factory=lambda: _env_bool("USE_SCRIPTED_ENGAGEMENT", True)
    )
    use_scripted_scraper: bool = field(
        default_factory=lambda: _env_bool("USE_SCRIPTED_SCRAPER", True)
    )
    scripted_action_timeout: float = field(
        default_factory=lambda: float(os.getenv("SCRIPTED_ACTION_TIMEOUT_S", "6.0"))
    )

    ingest_live_comment_prompt: bool = field(
        default_factory=lambda: _env_bool("INGEST_LIVE_COMMENT_PROMPT", True)
    )
    ingest_comment_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("INGEST_COMMENT_TIMEOUT_S", "180"))
    )
    hashtag_cooldown_days: float = field(
        default_factory=lambda: float(os.getenv("HASHTAG_COOLDOWN_DAYS", "2"))
    )

    # Analyzer: upload your own video and get an AI-generated title/caption/hashtags.
    analyzer_max_upload_mb: int = field(
        default_factory=lambda: int(os.getenv("ANALYZER_MAX_UPLOAD_MB", "300"))
    )

    def ensure_dirs(self) -> None:
        for path in (RAW_DIR, FILTERED_DIR, MEDIA_DIR, REPORTS_DIR, DATA_DIR, ANALYZER_UPLOAD_DIR):
            path.mkdir(parents=True, exist_ok=True)
        self.browser_user_data_dir.mkdir(parents=True, exist_ok=True)
        for slot in range(self.engage_profile_slots):
            self.profile_dir_for_slot(slot).mkdir(parents=True, exist_ok=True)

    def profile_dir_for_slot(self, slot: int) -> Path:
        base = self.browser_user_data_dir
        if slot <= 0:
            return base
        return base.parent / f"{base.name}-slot{slot}"


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
