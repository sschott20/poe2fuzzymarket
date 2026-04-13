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
    league: str = "Fate of the Vaal"
    cache_dir: str = str(Path.home() / ".cache" / "poe2market")
    cache_ttl_hours: int = 24
    max_fetch_items: int = 200

    @classmethod
    def load(cls) -> "Config":
        config = cls()

        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "rb") as f:
                data = tomllib.load(f)
            for key in ("poesessid", "league", "cache_dir"):
                if key in data:
                    setattr(config, key, str(data[key]))
            for key in ("cache_ttl_hours", "max_fetch_items"):
                if key in data:
                    setattr(config, key, int(data[key]))

        # Env vars take priority
        config.poesessid = os.environ.get("POE2_SESSID", config.poesessid)
        config.league = os.environ.get("POE2_LEAGUE", config.league)

        return config

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f'poesessid = "{self.poesessid}"',
            f'league = "{self.league}"',
            f'cache_dir = "{self.cache_dir}"',
            f"cache_ttl_hours = {self.cache_ttl_hours}",
            f"max_fetch_items = {self.max_fetch_items}",
        ]
        CONFIG_FILE.write_text("\n".join(lines) + "\n")
