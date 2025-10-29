"""
generic_config.py

This file defines the public, non-sensitive, and default configuration 
for the scraping project. All values here are placeholders or structure definitions.

NOTE: The actual working values must be defined in 'config.py', which 
should be excluded from version control (.gitignore).
"""
import logging
from pathlib import Path

# --- I/O AND LOGGING CONFIG ---
# Default log file name
LOG_FILENAME = "scraper_activity.log"
LOG_LEVEL = logging.INFO

# Database Configuration (for SQLite)
DB_FILENAME = Path("data/product_data.db")
DB_TABLE_NAME = "product_prices"

# CSV output file names
PRODUCT_OUTPUT_FILE = Path("output/product_data.csv")
AVAILABILITY_OUTPUT_FILE = Path("output/availability_data.csv")
FAILED_URLS_FILE = Path("output/failed_urls.csv")
SKU_FILE_NAME = Path("input/skus_to_filter.csv")


# --- SCRAPER BEHAVIOR CONSTANTS ---
# Max number of concurrent browser instances (adjust based on machine resources)
MAX_CONCURRENT_PAGES = 5

# Timeout for waiting for a page to load (in milliseconds)
PAGE_LOAD_TIMEOUT = 30000 

# Max retries for fetching a single product page
MAX_PRODUCT_RETRIES = 3 
MAX_PAGE_RETRIES = 2 # Retries for the listing (link collection) page

# Resource Blocking
BLOCK_RESOURCES = {'image', 'font', 'stylesheet', 'media'} # Empty this set in config.py if the site requires these resources

# Scrolling for dynamic loading (on listing pages)
SCROLL_ATTEMPTS = 5
SCROLL_PAUSE = 1.0 # Seconds between scroll attempts

# List of URL fragments to exclude during link collection
EXCLUDE_FRAGMENTS = ['#reviews', '#contact', '#policy']

# Selector used to find the JSON-LD script tag on product pages
JSON_LD_SELECTOR = 'script[type="application/ld+json"]'


# --- DUMMY SELECTORS (These must be overridden in config.py) ---
# Example: Selector for a product listing page URL
PRODUCT_LINK_SELECTOR = "a[href*='/p']"#"a.product-link"

# Example: Base URLs
BASE_URL = "https://example.com"
BRAND_PAGE = "https://example.com/category/all-products"

# Target brand to scrape (override in config.py)
TARGET_BRAND = "Example Brand"  # This should be the exact brand name as it appears in JSON-LD
