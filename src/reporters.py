from datetime import datetime
import logging
import csv
from typing import List, Tuple
from pathlib import Path

from src.models import FinalProductRecord
# from settings import FAILED_URLS_FILE, SKU_FILE_NAME
from generic_config import FAILED_URLS_FILE, BASE_URL

logger = logging.getLogger(__name__)

def _strip_base_url(url: str) -> str:
    """Remove BASE_URL prefix from a URL, returning only the relative path."""
    if BASE_URL and url.startswith(BASE_URL):
        return url[len(BASE_URL):]
    return url

def generate_csv_report(records: List[FinalProductRecord], output_path: Path, run_timestamp: str) -> None:
    """Generates a local CSV report of the final, clean data for this run only."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Price", "Old Price", "Promo Price", "Availability", "URL", "Changed"])
        
        for r in records:
            changed_str = "No"
            if r.is_significant_change:
                sign = "+" if r.price_change_percent > 0 else ""
                changed_str = f"Yes ({sign}{r.price_change_percent:.1%})"
                
            writer.writerow([
                r.normalized_sku, 
                r.price, 
                r.price_old, 
                r.price_promo, 
                r.availability_code, 
                _strip_base_url(r.url),
                changed_str
            ])
            
    logger.info(f"CSV report saved to {output_path.name} with {len(records)} records.")

def export_db_to_csv(db_path: str, output_path: Path) -> None:
    """Exports the entire price_history table from DuckDB to a CSV file."""
    import duckdb
    if not Path(db_path).exists():
        logger.error(f"Database {db_path} not found. Cannot export.")
        return

    try:
        with duckdb.connect(db_path) as con:
            logger.info(f"Exporting DB {db_path} to {output_path}...")
            # Use DuckDB's COPY command for efficient CSV export
            con.execute(f"COPY price_history TO '{output_path}' (HEADER, DELIMITER ',')")
            logger.info(f"Database exported successfully to {output_path}")
    except Exception as e:
        logger.error(f"Failed to export database: {e}")

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

