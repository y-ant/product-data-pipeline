import asyncio
import logging
from pathlib import Path
import random
import csv
import argparse
import os
from dotenv import load_dotenv
from datetime import datetime
from typing import List, Tuple

from playwright.async_api import async_playwright, Page, BrowserContext


# Import from configuration
from generic_config import (
    URL_LIST_PATH, OUTPUT_DIR, DB_FILENAME,
    CONCURRENCY_LIMIT, LOG_LEVEL,
    get_random_ua, BASE_URL
)

# Imports from src/
from src.extraction import scrape_single_product, _block_resources, filter_scraped_data
from src.loader import insert_data
from src.reporters import log_failed_urls, generate_csv_report

# --- LOGGING SETUP ---
timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M")
log_path = OUTPUT_DIR / f"scrape_{timestamp}.log"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_path), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def check_environment():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        logger.info("🚀 Running in: GITHUB ACTIONS")
    else:
        logger.info("💻 Running in: LOCAL ENVIRONMENT")
        logger.info("⚠️ Ensure your local .env file or export is set.")

# --- WORKER FUNCTION ---

async def task_processor(
    pw_instance,
    worker_id: int,
    queue: asyncio.Queue,
    all_scraped_items: List, 
    failed_log_data: List[Tuple[str, str, str]],
    stop_flag: asyncio.Event
) -> None:
    browser = None
    try:
        # Launching without proxy
        browser = await pw_instance.chromium.launch(
            headless=True,
            args=['--disable-dev-shm-usage', '--no-sandbox']
        )

        context: BrowserContext = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=get_random_ua()
        )
        
        page: Page = await context.new_page()
        
        await _block_resources(page)
        
        logger.info(f"Worker {worker_id} started.")

        while not stop_flag.is_set():
            try:
                relative_url = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Combine secret BASE_URL with path
            full_url = f"{BASE_URL}{relative_url}"

            try:
                logger.info(f"Worker {worker_id} processing: {full_url}")
                prod_item = await scrape_single_product(page, full_url, failed_log_data, stop_flag)
                
                if prod_item:
                    all_scraped_items.append(prod_item)
                    
            except Exception as e:
                logger.error(f"Worker {worker_id} failed on {full_url}: {e}")
                failed_log_data.append((relative_url, "SCRAPE_ERROR", str(e)))
            finally:
                queue.task_done()

            # Random delay to look human
            await asyncio.sleep(1 + random.random() * 2)
                
    except asyncio.CancelledError:
        logger.info(f"Worker {worker_id} requested to stop.")
    except Exception as e:
        # exc_info=True will print the traceback so we can see which line is "not callable"
        logger.critical(f"Worker {worker_id} CRASHED: {e}", exc_info=True)
    finally:
        if browser:
            await browser.close()

# --- ORCHESTRATOR ---

async def run_scrape_pipeline(limit: int = None):
    logger.info("-" * 30)
    logger.info(f"Pipeline Start | Concurrency: {CONCURRENCY_LIMIT}")
    
    if not BASE_URL:
        logger.error("FATAL: BASE_URL environment variable is not set.")
        return
    
    if not URL_LIST_PATH.exists():
        logger.error(f"Critical Error: {URL_LIST_PATH} not found.")
        return

    # Load relative paths from CSV
    with open(URL_LIST_PATH, mode='r', encoding='utf-8') as f:
        reader = csv.reader(f)
        urls = [row[0] for row in reader if row]

    if limit:
        urls = urls[:limit]
        logger.info(f"Limiting execution to first {limit} URLs.")
    else:
        logger.info(f"Total URLs to process: {len(urls)}")

    url_queue = asyncio.Queue()
    for u in urls:
        await url_queue.put(u)
    
    all_scraped_items = []
    failed_log_data = []
    stop_flag = asyncio.Event()

    async with async_playwright() as pw:
        tasks = []
        for i in range(CONCURRENCY_LIMIT):
            tasks.append(asyncio.create_task(
                task_processor(pw, i+1, url_queue, all_scraped_items, failed_log_data, stop_flag)
            ))

        await url_queue.join()
        
        # Cleanup tasks
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Finalizing
    logger.info(f"Scraping complete. Items found: {len(all_scraped_items)}")
    final_records = filter_scraped_data(all_scraped_items, None)
    insert_data(final_records, str(DB_FILENAME))

    # Generate CSV with this run's prices only
    csv_filename = f"prices_{timestamp}.csv"
    csv_path = OUTPUT_DIR / csv_filename
    generate_csv_report(final_records, csv_path, timestamp)
    logger.info(f"CSV artifact saved: {csv_path}")

    if failed_log_data:
        fail_filename = f"failed_url_list_{timestamp}.csv"
        log_failed_urls(failed_log_data, str(OUTPUT_DIR / fail_filename))

if __name__ == "__main__":
    check_environment()

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cron", action="store_true")
    args = parser.parse_args()

    async def main():
        if args.cron:
            delay = abs(random.gauss(0, 3600))
            logger.info(f"Cron Schedule: Delaying start by {delay/60:.1f} minutes.")
            await asyncio.sleep(delay)
            
        await run_scrape_pipeline(limit=args.limit)

    success = False
    try:
        asyncio.run(main())
        success = True
    except KeyboardInterrupt:
        logger.warning("Manually interrupted by user.")
    finally:
        if success:
            logger.info("Scrape pipeline finished successfully.")
        else:
            logger.info("Scrape pipeline finished with interruption or error. Check logs for details.")