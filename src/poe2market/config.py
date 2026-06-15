import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        import tomli as tomllib  # type: ignore[import,no-redef]


CONFIG_DIR = Path.home() / ".config" / "poe2market"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    poesessid: str = ""
    league: str = "Runes of Aldur"
    cache_dir: str = str(Path.home() / ".cache" / "poe2market")
    cache_ttl_hours: int = 24
    max_fetch_items: int = 200
    auto_sync_minutes: int = 20  # background history sync interval; 0 disables
    tracker_minutes: int = 13    # background boots sale-tracker poll interval; 0 disables
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-6"

    @classmethod
    def load(cls) -> "Config":
        config = cls()

        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "rb") as f:
                    data = tomllib.load(f)
            except (tomllib.TOMLDecodeError, OSError):
                # Corrupt/unreadable config shouldn't brick the app; fall back
                # to defaults + env vars. (A bad value can be re-saved to fix.)
                data = {}
            for key in (
                "poesessid",
                "league",
                "cache_dir",
                "anthropic_api_key",
                "anthropic_model",
            ):
                if key in data:
                    setattr(config, key, str(data[key]))
            for key in ("cache_ttl_hours", "max_fetch_items", "auto_sync_minutes",
                        "tracker_minutes"):
                if key in data:
                    setattr(config, key, int(data[key]))

        # Env vars take priority
        config.poesessid = os.environ.get("POE2_SESSID", config.poesessid)
        config.league = os.environ.get("POE2_LEAGUE", config.league)
        config.anthropic_api_key = os.environ.get(
            "ANTHROPIC_API_KEY", config.anthropic_api_key
        )

        return config

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f"poesessid = {_toml_str(self.poesessid)}",
            f"league = {_toml_str(self.league)}",
            f"cache_dir = {_toml_str(self.cache_dir)}",
            f"cache_ttl_hours = {self.cache_ttl_hours}",
            f"max_fetch_items = {self.max_fetch_items}",
            f"auto_sync_minutes = {self.auto_sync_minutes}",
            f"tracker_minutes = {self.tracker_minutes}",
            f"anthropic_api_key = {_toml_str(self.anthropic_api_key)}",
            f"anthropic_model = {_toml_str(self.anthropic_model)}",
        ]
        CONFIG_FILE.write_text("\n".join(lines) + "\n")


def _toml_str(value: str) -> str:
    """Serialize a string as a valid TOML basic string.

    Escapes backslashes and double quotes — important on Windows, where paths
    like ``C:\\Users\\...`` would otherwise be read as invalid escape sequences.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
