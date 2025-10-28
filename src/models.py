from dataclasses import dataclass
from typing import Literal, Optional, List, Tuple
from datetime import datetime

# --- Custom Types for Clarity ---
# Defines the set of possible anti-scraping statuses
DetectionStatus = Literal["OK", "HTTP_BLOCKED", "CAPTCHA_DETECTED", "SILENT_FAILURE"]

# Defines the tuple structure returned by the filter step
NormalizedData = Tuple[str, str, str, str, str, DetectionStatus]

# --- Dataclasses for Data Modeling ---

@dataclass
class ScrapedItem:
    """Represents a single scraped item, used before the filter/loader steps."""
    sku_raw: str
    price_raw: str
    availability_raw: str
    url: str
    timestamp: datetime
    # Optional field for status tracking
    detection_status: DetectionStatus = "OK"


@dataclass
class FinalProductRecord:
    """Represents a product record after normalization and ready for database insertion."""
    normalized_sku: str
    price: float
    availability_code: str
    url: str
    timestamp: datetime
    detection_status: DetectionStatus
