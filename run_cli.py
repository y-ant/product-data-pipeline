import asyncio
import logging
import random
import os
import numpy as np # Used for splitting the list
from typing import List, Tuple, Optional
from datetime import datetime

from playwright.async_api import async_playwright, Page, BrowserContext

# Import configuration from settings (ASSUMING YOUR settings.py EXISTS)
# NOTE: Ensure CONCURRENCY_LIMIT is defined in your settings.py
from settings import (
    BASE_URL, BRAND_PAGE, MAX_PAGE_RETRIES, SCROLL_ATTEMPTS, SCROLL_PAUSE,
    PAGE_LOAD_TIMEOUT, EXCLUDE_FRAGMENTS, JSON_LD_SELECTOR, PRODUCT_LINK_SELECTOR,
    MAX_PRODUCT_RETRIES, BLOCK_RESOURCES, SKU_FILE_NAME, FAILED_URLS_FILE,
    TARGET_BRAND, DB_FILENAME, LOG_FILENAME, LOG_LEVEL, PROXY_ROTATION_INTERVAL, CONCURRENCY_LIMIT
)

# Import logic from the modular structure (ASSUMING YOUR src/ MODULES EXIST)
from src.extraction import (
    collect_product_urls, scrape_single_product, filter_scraped_data, _block_resources
)
from src.loader import setup_database, insert_data
from src.reporters import generate_csv_report, read_skus_from_file, log_failed_urls
from src.models import ScrapedItem
from src.proxy_manager import ProxyRotator

# --- 1. SETUP ---

# Set up logging using Pathlib
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Simple list of User Agents for demonstration (expand this in production)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/117.0',
    # Add more diverse, real UAs
]

# --- 2. WORKER FUNCTION (N-Context Architecture) ---

async def task_processor(
    pw_instance,
    worker_id: int,
    urls_to_process: List[str],
    all_scraped_items: List[ScrapedItem], # Shared list for results
    failed_log_data: List[Tuple[str, str, str]],
    stop_flag: asyncio.Event,
    proxy_manager: ProxyRotator
) -> None:
    """
    A long-lived worker that creates one isolated browser context and processes 
    a dedicated batch of URLs sequentially using that context.
    """
    # 1. Isolation & Stealth Configuration
    random_ua = random.choice(USER_AGENTS)
    proxy_config = proxy_manager.get_proxy_config() # Get a fresh proxy for this worker
    proxy_server_log = proxy_config.get('server', 'None (No Proxy)') if proxy_config else 'None (No Proxy)'
    # FIX 2: Only include the 'proxy' argument if proxy_config is not None
    launch_args = {
        'headless': True,
        'args': [
            '--disable-dev-shm-usage', 
            '--no-sandbox', 
            '--disable-blink-features=AutomationControlled'
        ]
    }
    if proxy_config:
        launch_args['proxy'] = proxy_config
    logger.info(f"Worker {worker_id}: Starting with {len(urls_to_process)} URLs. Proxy: {proxy_server_log}")

    # 2. Context Creation (Isolation and Reuse)
    browser = await pw_instance.chromium.launch(
        headless=True,
        proxy=proxy_config, # Apply proxy once for the entire worker
        args=[
            '--disable-dev-shm-usage', 
            '--no-sandbox', 
            '--disable-blink-features=AutomationControlled'
        ]
    )
    context: BrowserContext = await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=random_ua # Apply unique UA for the context #Q: number of USER_AGENTS should be as the number of workers?
    )
    page: Page = await context.new_page()
    await _block_resources(page) # Apply resource blocking once per context #Q: what is it for?

    # 3. Sequential Processing (URL Batch) #Q: what is the point of the await for scrape_single_product in here?
    for idx, url in enumerate(urls_to_process, start=1):
        if stop_flag.is_set():
            logger.warning(f"Worker {worker_id} stopping early.")
            break

        try:
            prod_item = await scrape_single_product(page, url, failed_log_data, stop_flag)
            
            if prod_item:
                # Append to the shared list (safe in asyncio for simple append)
                all_scraped_items.append(prod_item) 
                
        except Exception as e:
            logger.error(f"Worker {worker_id} failed on {url}: {e}")
            failed_log_data.append((url, "SCRAPE_ERROR", str(e)))

        # 4. Random Delay (Politeness and Stealth)
        delay = 0.5 + random.random() * 1.5 # Example: 0.5s to 2.0s between requests
        await asyncio.sleep(delay)
        
        if idx % 10 == 0:
             logger.info(f"Worker {worker_id} STATUS: Processed {idx}/{len(urls_to_process)} URLs. Scraped items: {len(all_scraped_items)}")

    # 5. Cleanup: Close Context and Browser
    await context.close() #Q: Why do we need to close both context and browser?
    await browser.close()
    logger.info(f"Worker {worker_id}: Finished processing batch.")


# --- 3. MAIN PIPELINE FUNCTION (ORCHESTRATOR) ---

async def run_scrape_pipeline(max_pages: int = -1, use_sku_filter: bool = False, use_p_url: bool = False, filename_x: str = "ProvideFilename.csv", filename_y: str = "ProvideFilename.csv"):
    if filename_y == "ProvideFilename.csv":
        filename_y = filename_x
        
    setup_database(DB_FILENAME)
    skus_to_filter: Optional[List[str]] = read_skus_from_file() if use_sku_filter else None
    
    if use_sku_filter and not skus_to_filter:
        logger.error("SKU filter requested but file is empty. Aborting.")
        return
        
    stop_flag = asyncio.Event() 
    all_scraped_items: List[ScrapedItem] = []
    # Note: `failed_log_data` must be defined outside the worker loop to collect results
    failed_log_data: List[Tuple[str, str, str]] = [] 

    logger.info("-" * 50)
    logger.info(f"Pipeline Starting: Concurrency={CONCURRENCY_LIMIT}")
    logger.info("-" * 50)
    
    try:
        # Initialize proxy rotator
        proxy_rotator = ProxyRotator()
        
        product_urls = []
        
        # --- URL COLLECTION (Sequential/Initial Phase) ---
        # The URL collection must run sequentially before the concurrent scraping
        async with async_playwright() as pw:
            
            # --- URL Collection Logic (using your existing structure for this phase) ---
            if use_p_url:
                # Read URLs from file
                import csv
                logger.info(f"Reading URLs from input/{filename_x}")
                with open(f'input/{filename_x}', newline='') as f:
                    data = csv.reader(f)
                    reader = list(data)
                product_urls = [row[0] for row in reader if row]
            else:
                # Launch a dedicated browser for URL collection if needed # Q: is it really needed? I bet so
                browser_coll = await pw.chromium.launch(headless=True, proxy=proxy_rotator.get_proxy_config())
                context_coll = await browser_coll.new_context(user_agent=random.choice(USER_AGENTS))
                page_coll = await context_coll.new_page()
                await _block_resources(page_coll)

                try:
                    logger.info(f"Attempting to navigate to {BRAND_PAGE} for URL collection.")
                    await page_coll.goto(BRAND_PAGE, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                    max_pages_to_collect = -1 if use_sku_filter else max_pages
                    product_urls = await collect_product_urls(page_coll, max_pages_to_collect, stop_flag)
                except Exception as e:
                    logger.error(f"Failed to collect initial URLs from {BRAND_PAGE}: {e}")
                    return
                finally:
                    await context_coll.close()
                    await browser_coll.close()
                    
            if not product_urls:
                logger.warning("No product URLs found. Exiting.")
                return
            # Q: do we need to close the browser_coll and context_coll, page_coll-No

            # --- CONCURRENT SCRAPING (N-Context Worker Pool) ---
            
            N_WORKERS = CONCURRENCY_LIMIT
            logger.info(f"Starting concurrent scraping of {len(product_urls)} URLs with {N_WORKERS} workers...")

            # 1. Split Links into N batches
            if len(product_urls) > N_WORKERS:
                # Use numpy for splitting array into roughly equal parts (Requirement 3)
                url_batches = np.array_split(np.array(product_urls), N_WORKERS)
                # Filter out empty arrays and convert back to list of lists
                url_batches = [list(batch) for batch in url_batches if len(batch) > 0]
            else:
                 # If fewer URLs than workers, treat each URL as its own batch
                 url_batches = [[url] for url in product_urls]
                 N_WORKERS = len(product_urls)
            
            # 2. Launch N Worker Tasks (one task_processor per batch)
            tasks = []
            for worker_id, batch in enumerate(url_batches):
                task = asyncio.create_task(
                    task_processor(
                        pw,
                        worker_id=worker_id + 1,
                        urls_to_process=batch,
                        all_scraped_items=all_scraped_items, 
                        failed_log_data=failed_log_data,
                        stop_flag=stop_flag,
                        proxy_manager=proxy_rotator
                    )
                )
                tasks.append(task)
                
            # 3. Wait for all N workers to complete
            await asyncio.gather(*tasks) #Q does asyncio.gather actually runs the tasks?

            logger.info(f"Concurrent scraping finished. Total scraped items: {len(all_scraped_items)}")
            
        # --- 4. Transformation, Loading, and Reporting ---
        final_records = filter_scraped_data(all_scraped_items, skus_to_filter)
        insert_data(final_records, DB_FILENAME)
        
        generate_csv_report(final_records, DB_FILENAME.parent / f"final_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        
        if failed_log_data:
            logger.info(f"Logging {len(failed_log_data)} failed URLs")
            log_failed_urls(failed_log_data, filename_y)
        
        logger.info("Pipeline completed successfully.")

    except Exception as e:
        logger.critical(f"Pipeline CRASHED: {e}", exc_info=True)


if __name__ == "__main__":

    N = 10 # Example: Limit URL collection for testing
    
    logger.info(f"Example execution: Using CONCURRENCY_LIMIT={CONCURRENCY_LIMIT}")
    try:
        # NOTE: Ensure you have a file named "extended_list.txt" in an "input" directory
        asyncio.run(run_scrape_pipeline(max_pages=N, use_sku_filter=False, use_p_url=True, filename_x="extended_list.txt"))
    except KeyboardInterrupt:
        logger.warning("Pipeline manually interrupted.")