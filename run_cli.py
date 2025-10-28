import asyncio
import logging
import random
import os
from typing import List, Tuple, Optional
from datetime import datetime

from playwright.async_api import async_playwright, Page

# Import logic from the modular structure
from generic_config import LOG_FILENAME, LOG_LEVEL, DB_FILENAME
from src.extraction import (
    collect_product_urls, scrape_single_product, filter_scraped_data, _block_resources
)
from src.loader import setup_database, insert_data
from src.reporters import generate_csv_report, read_skus_from_file
from src.models import ScrapedItem

# Attempt to load private config, otherwise use generic placeholders
try:
    from config import * # Load actual BASE_URL, BRAND_PAGE, etc.
    print("Loaded private config.py")
except ImportError:
    print("Warning: Using generic_config.py placeholders. Create config.py for real run.")

# Set up logging using Pathlib
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

async def run_scrape_pipeline(max_pages: int = -1, use_sku_filter: bool = False):
    """
    Main asynchronous pipeline function (simulating a Prefect Flow run).
    """
    # 1. Setup
    setup_database(DB_FILENAME)
    
    skus_to_filter: Optional[List[str]] = read_skus_from_file() if use_sku_filter else None
    
    if use_sku_filter and not skus_to_filter:
        logger.error("SKU filter requested but file is empty. Aborting.")
        return
        
    stop_flag = asyncio.Event() # For stopping the process gracefully
    all_scraped_items: List[ScrapedItem] = []
    failed_urls_log: List[Tuple[str, str, str]] = [] # (URL, Reason, Details)

    logger.info("-" * 50)
    logger.info(f"Pipeline Starting: Pages={max_pages if max_pages != -1 else 'ALL'}, SKU Filter={use_sku_filter}")
    logger.info("-" * 50)
    
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await _block_resources(page)

            # 2. Extraction: URL Collection
            max_pages_to_collect = -1 if use_sku_filter else max_pages
            product_urls = await collect_product_urls(page, max_pages_to_collect, stop_flag)
            
            if not product_urls:
                logger.warning("No product URLs found. Exiting.")
                return

            # 3. Extraction: Product Scraping Loop (Sequential)
            urls_to_scrape = list(set(product_urls))
            logger.info(f"Processing {len(urls_to_scrape)} unique product URLs.")
            
            # Simple in-memory CSV writer for logging failed attempts during scrape
            failed_log_data = [] 
            
            for idx, url in enumerate(urls_to_scrape, start=1):
                # Placeholder for writing failed log to list for final write (since we can't write in async)
                
                # --- This is where we would implement a concurrent worker pool for parallel scraping ---
                prod_item = await scrape_single_product(page, url, failed_log_data, stop_flag)

                if prod_item:
                    all_scraped_items.append(prod_item)
                
                if idx % 100 == 0:
                    logger.info(f"STATUS: Processed {idx}/{len(urls_to_scrape)} items. Scraped items: {len(all_scraped_items)}")
                    
                # Polite delay (Sequential mode only)
                await asyncio.sleep(0.15 + random.random() * 0.25) 
                
            await browser.close()
            
            # 4. Transformation and Loading (T & L)
            final_records = filter_scraped_data(all_scraped_items, skus_to_filter)
            insert_data(final_records, DB_FILENAME)
            
            # 5. Reporting (D)
            generate_csv_report(final_records, DB_FILENAME.parent / f"final_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            # reporters.upload_to_google_sheets(final_records, "PREFECT_SECRET_KEY") # Placeholder
            
            logger.info("Pipeline completed successfully.")

    except Exception as e:
        logger.critical(f"Pipeline CRASHED: {e}", exc_info=True)
        # Handle cleanup on crash (e.g., send crash notification)

if __name__ == "__main__":
    # Ensure a basic SKU file exists for demonstration
    if not os.path.exists("skus_input.csv"):
        with open("skus_input.csv", 'w', newline='', encoding='utf-8') as f:
            f.write("# One SKU per line\n# 1234567\n# 98-7654-3210")
            
    # Example execution: Scrape the first 5 pages, no SKU filter
    try:
        asyncio.run(run_scrape_pipeline(max_pages=2, use_sku_filter=False))
    except KeyboardInterrupt:
        logger.warning("Pipeline manually interrupted.")
