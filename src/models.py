import re
# from config import VALID_COLLECTIONS
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional, List, Tuple
from datetime import datetime
from generic_config import VALID_COLLECTIONS

# --- Custom Types for Clarity ---
# Defines the set of possible anti-scraping statuses
DetectionStatus = Literal["OK", "HTTP_BLOCKED", "CAPTCHA_DETECTED", "SILENT_FAILURE"]

# Defines the tuple structure returned by the filter step
NormalizedData = Tuple[str, str, str, str, str, DetectionStatus]

# --- Pydantic Items for Data Modeling ---

class ScrapedItem(BaseModel):
    """Represents a single scraped item, used before the filter/loader steps."""
    # Core Identification
    sku_raw: str = Field(..., min_length=1)
    collection: str = Field("General", description="The product collection")
    @field_validator("collection", mode="before")
    @classmethod
    def clean_and_lookup_collection(cls, v: str) -> str:
        """
        1. Strips language prefixes.
        2. Validates against the extensive look-up list.
        """
        if not isinstance(v, str):
            return "General"

        clean_name = re.sub(r'^(Колекція|Коллекция|Collection)[:\s]*', '', v, flags=re.IGNORECASE)
        clean_name = clean_name.strip()

        # 2. Look-up Validation
        # If the cleaned name is in our 'Master List', return it.
        # Otherwise, we can choose to return "General" or log a warning.
        if clean_name in VALID_COLLECTIONS:
            return clean_name
            
        # Optional: Log if we found a new collection not in our list
        # logger.warning(f"New collection detected: {clean_name}")
        return clean_name # Or return "General" if you want strict filtering
    
    # Pricing Information
    price_raw: float = Field(..., ge=-1.0) 
    price_old_raw: Optional[float] = Field(None, ge=-1.0)
    price_promo_raw: Optional[float] = Field(None, ge=-1.0)
    # Availability and URL
    availability_raw: str
    url: str

    # Optional field for status tracking
    detection_status: DetectionStatus = "OK"


class FinalProductRecord(BaseModel):
    """Represents a product record after normalization and ready for database insertion."""
    normalized_sku: str
    collection: str = Field("General", description="The product collection")
    price: float
    price_old: float
    price_promo: float
    availability_code: str
    url: str
    detection_status: DetectionStatus
    
    # Tracking fields (Used for reports, but not stored in DB)
    is_significant_change: bool = False
    price_change_percent: float = 0.0