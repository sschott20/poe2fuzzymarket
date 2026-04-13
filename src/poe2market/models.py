from dataclasses import dataclass, field


@dataclass
class StatFilter:
    """A stat filter for trade API queries."""
    stat_id: str
    min_value: float | None = None
    max_value: float | None = None
    weight: float = 1.0


@dataclass
class StatValue:
    """A single stat on an item with its parsed numerical value."""
    stat_id: str
    text: str
    value: float


@dataclass
class Listing:
    """A trade listing with parsed item data."""
    item_id: str
    price: float
    currency: str
    stats: list[StatValue] = field(default_factory=list)
    name: str = ""
    base_type: str = ""
    ilvl: int = 0
    listed_at: str = ""
    account_name: str = ""
    whisper: str = ""
    icon: str = ""


@dataclass
class StatCoefficient:
    """Result of price regression for a single stat."""
    stat_id: str
    text: str
    coefficient: float
    std_error: float = 0.0
    sample_count: int = 0


@dataclass
class Deal:
    """A scored listing representing a potential deal."""
    listing: Listing
    weighted_score: float
    value_ratio: float
    stat_contributions: dict[str, float] = field(default_factory=dict)
