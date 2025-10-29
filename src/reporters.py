from datetime import datetime
import logging
import csv
from typing import List, Tuple
from pathlib import Path

from src.models import FinalProductRecord
# from settings import FAILED_URLS_FILE, SKU_FILE_NAME
from generic_config import FAILED_URLS_FILE#, SKU_FILE_NAME, VALID_COLLECTIONS

logger = logging.getLogger(__name__)

def generate_csv_report(records: List[FinalProductRecord], output_path: Path) -> None:
    """Generates a local CSV report of the final, clean data."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Price", "Old Price", "Promo Price", "Availability", "URL", "Timestamp", "Detection_Status"])
        writer.writerows([
            (r.normalized_sku, r.price, r.price_old, r.price_promo, r.availability_code, r.url, r.timestamp.isoformat(), r.detection_status)
            for r in records
        ])
    logger.info(f"Local CSV report saved to {output_path.name}")

# --- Placeholder for Google Sheets ---
def upload_to_google_sheets(records: List[FinalProductRecord], service_account_json: str) -> None:
    """
    D-Step: Connects to Google Sheets API and updates the report tab.
    
    NOTE: This is a placeholder. You would implement gspread or a similar library here,
    using the service_account_json (passed from a Prefect Secret) for authentication.
    """
    # Implementation requires:
    # 1. pip install gspread google-auth
    # 2. Reading the service_account_json
    # 3. Connecting to the target spreadsheet
    # 4. Overwriting the data tab
    
    logger.info(f"Delivering {len(records)} records to Google Sheets... (Placeholder)")
    # Example: gspread_client = gspread.service_account(file_name=service_account_json)
    # Example: sheet = gspread_client.open("Your Market Data Dashboard").worksheet("Raw Data")
    # Example: sheet.clear()
    # Example: sheet.append_rows(...)
    pass

def log_failed_urls(failed_urls: List[Tuple[str, str, str]], filename_y: str) -> None:
    """Logs the final list of failed/fallback URLs to a separate CSV."""
    with open(f"{FAILED_URLS_FILE}_{datetime.now().strftime('%Y%m%d')}.csv", 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["URL", "Reason", "Details"])
        writer.writerows(failed_urls)
    logger.info(f"Failed URLs logged to {FAILED_URLS_FILE.name}")

