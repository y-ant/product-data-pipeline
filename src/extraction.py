import re
import csv
import json
import logging
import asyncio
from datetime import datetime
from typing import List, Set, Optional, Dict, Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

# Import configuration from settings
from settings import (
    BASE_URL, BRAND_PAGE, MAX_PAGE_RETRIES, SCROLL_ATTEMPTS, SCROLL_PAUSE, PAGE_LOAD_TIMEOUT,
    EXCLUDE_FRAGMENTS, JSON_LD_SELECTOR, PRODUCT_LINK_SELECTOR, MAX_PRODUCT_RETRIES,
    BLOCK_RESOURCES, SKU_FILE_NAME, FAILED_URLS_FILE, TARGET_BRAND
)

from src.models import ScrapedItem, FinalProductRecord, DetectionStatus, NormalizedData

logger = logging.getLogger(__name__)

# --- Core Utility Functions (T-Step) ---

def _sanitize_url(url: str) -> str:
    """Remove fragment identifiers (#reviews-col, etc.)"""
    if '#' in url:
        return url.split('#', 1)[0]
    return url

def get_product_id_from_url(url: str) -> Optional[str]:
    """Extract numeric product ID from URL (e.g., '2427637' from '/p2427637')"""
    match = re.search(r'/p(\d+)/?(?:#.*)?$', url)
    return match.group(1) if match else None

def is_valid_product_url(href: str) -> bool:
    """Check if URL is a valid product URL (not policy/external/protocol link)"""
    if not href:
        return False
    href_lower = href.lower()
    
    if '://' in href and not href.startswith('http'):
        return False
    if any(fragment.lower() in href_lower for fragment in EXCLUDE_FRAGMENTS):
        return False
    if '/p' not in href:
        return False

    return True

def is_valid_sku(sku_raw: str) -> bool:
    """Check if SKU is valid based on length and numeric content after cleaning."""
    if not sku_raw or sku_raw.startswith("N/A"):
        return False
    
    normalized = sku_raw.replace('-', '').replace(' ', '').strip()
    
    if not normalized.isdigit():
        return False

    # Check for reasonable length (6 to 15 digits)
    return 6 <= len(normalized) <= 15

def normalize_price(price_raw: str) -> float:
    """
    Cleans raw price string and converts it to a float.
    Returns -1.0 if conversion fails.
    """
    try:
        # Remove currency symbols (₴), commas, spaces
        cleaned = re.sub(r'[^\d.]', '', price_raw)
        return float(cleaned)
    except:
        return -1.0

def normalize_sku(sku_raw: str) -> str:
    """Removes non-alphanumeric characters from the SKU."""
    return re.sub(r'[^a-zA-Z0-9]', '', sku_raw).strip()

def filter_scraped_data(data: List[ScrapedItem], skus_to_check: List[str] = None) -> List[FinalProductRecord]:
    """
    T-Step: Filter, normalize, and validate scraped data.
    1. Validates and normalizes SKU.
    2. Filters against a whitelist if provided.
    3. Removes duplicates based on normalized SKU.
    """
    filtered_records: List[FinalProductRecord] = []
    seen_skus: Set[str] = set()

    # Pre-normalize whitelist SKUs once
    sku_whitelist_normalized: Set[str] = set(normalize_sku(s) for s in skus_to_check) if skus_to_check else set()
    is_filtering_by_sku = bool(skus_to_check)

    for item in data:
        if not is_valid_sku(item.sku_raw):
            logger.debug(f"Invalid SKU rejected: {item.sku_raw} from {item.url}")
            continue

        normalized_sku = normalize_sku(item.sku_raw)

        if is_filtering_by_sku and normalized_sku not in sku_whitelist_normalized:
            logger.debug(f"SKU not in whitelist: {item.sku_raw}")
            continue

        if normalized_sku in seen_skus:
            logger.debug(f"Duplicate SKU skipped: {normalized_sku}")
            continue

        seen_skus.add(normalized_sku)
        
        # Final normalized record creation
        record = FinalProductRecord(
            normalized_sku=normalized_sku,
            price=normalize_price(item.price_raw),
            price_old=normalize_price(item.price_old_raw),
            availability_code=item.availability_raw,
            url=item.url,
            timestamp=item.timestamp,
            detection_status=item.detection_status
        )
        filtered_records.append(record)

    return filtered_records

# --- Playwright Helpers (E-Step) ---

async def _block_resources(page: Page) -> None:
    """Blocks non-essential resources for faster loading."""
    await page.route("**/*", lambda route: asyncio.create_task(
        route.abort()) if route.request.resource_type in BLOCK_RESOURCES else asyncio.create_task(route.continue_())
    )

async def parse_product_jsonld(page: Page) -> Optional[Dict[str, str]]:
    """Parse JSON-LD structured data for product info (sku, price, availability)."""
    # Try different selectors to find JSON-LD scripts
    selectors = [
        'script[type="application/ld+json"]'  # Only using the standard double-quoted version
    ]
    
    all_scripts = []
    for selector in selectors:
        scripts = await page.query_selector_all(selector)
        all_scripts.extend(scripts)
    
    for s in all_scripts:
        try:
            # Try both innerHTML and innerText as some pages might format differently
            text = await s.evaluate('el => el.innerHTML || el.innerText')
            text = text.strip()
            if not text:
                continue

            data = json.loads(text)
            candidates = data if isinstance(data, list) else [data]

            for item in candidates:
                if not isinstance(item, dict):
                    continue
                    
                # Check if it's a product type
                if item.get("@type") != "Product":
                    continue

                # Check for brand
                brand_data = item.get("brand", {})
                if isinstance(brand_data, dict):
                    brand_name = brand_data.get("name", "").strip()
                    # Handle HTML entity in brand name
                    brand_name = brand_name.replace("&amp;", "&")
                    if brand_name != TARGET_BRAND:
                        logger.debug(f"Skipping non-{TARGET_BRAND} product. Brand: {brand_name}")
                        continue

                # Try different possible SKU fields
                sku = None
                for sku_field in ["sku", "productID", "mpn"]:
                    sku_raw = str(item.get(sku_field, "")).strip()
                    if sku_raw and sku_raw.lower() != "none" and len(sku_raw) >= 3:
                        sku = sku_raw
                        break

                if not sku:
                    continue

                # Handle offers data
                offers = item.get("offers", {}) or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                elif not isinstance(offers, dict):
                    continue

                # Get current price and handle different formats
                price = str(offers.get("price", "-1")).strip()
                
                # Try to get old price from DOM
                price_old = "-1"
                try:
                    old_price_selectors = [
                        ".price__old",       # Primary selector
                        ".price--old",       # Alternative format
                        ".original-price",   # Another common pattern
                        ".product-price-old" # Backup selector
                    ]
                    for selector in old_price_selectors:
                        old_price_element = await page.query_selector(selector)
                        if old_price_element:
                            price_old_text = await old_price_element.inner_text()
                            # Clean up the price (remove currency symbols, spaces, etc.)
                            price_old = re.sub(r'[^\d.]', '', price_old_text)
                            if price_old:  # If we found a valid price, break the loop
                                break
                except Exception as e:
                    logger.debug(f"Error extracting old price from DOM: {str(e)}")

                logger.debug(f"Found JSON-LD data: SKU={sku}, Price={price}")
                # Include brand information in the returned data
                return {
                    "sku_raw": sku,
                    "price_raw": price,
                    "price_old_raw": price_old,
                    "availability_raw": "-1",  # Will be set by DOM extraction later
                    "brand": brand_name  # Include brand for logging/debugging
                }
                
        except json.JSONDecodeError as e:
            logger.debug(f"JSON decode error: {str(e)}")
        except Exception as e:
            logger.debug(f"Error parsing JSON-LD: {str(e)}")
            
    return None

# --- Main Scraping Logic (E-Step) ---

async def collect_product_urls(page: Page, max_pages: int = -1, stop_flag: asyncio.Event = None) -> List[str]:
    """
    Collect unique, valid product URLs from brand pagination pages.
    """
    urls: List[str] = []
    seen_product_ids: Set[str] = set()
    pnum: int = 1
    max_pages_limit: float = max_pages if max_pages != -1 else float('inf')

    while pnum <= max_pages_limit:
        if stop_flag and stop_flag.is_set():
            break

        page_url: str = f"{BRAND_PAGE}?page={pnum}"

        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            try:
                logger.info(f"Loading index page {pnum} (attempt {attempt})")
                await page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                await asyncio.sleep(0.3)#TODO

                prev_count: int = -1
                current_urls_on_page_load: int = len(urls)

                for _ in range(SCROLL_ATTEMPTS):
                    anchors = await page.query_selector_all(PRODUCT_LINK_SELECTOR)

                    for a in anchors:
                        href = await a.get_attribute("href") or ""
                        if not is_valid_product_url(href):
                            continue

                        href = BASE_URL + href if not href.startswith("http") else href
                        href_sanitized = _sanitize_url(href)
                        product_id = get_product_id_from_url(href_sanitized)

                        if product_id and product_id not in seen_product_ids:
                            seen_product_ids.add(product_id)
                            urls.append(href_sanitized)

                    if len(seen_product_ids) == prev_count:
                        break # No new URLs found after scrolling
                    prev_count = len(seen_product_ids)

                    # Scroll down to load lazy-loaded content
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    await asyncio.sleep(SCROLL_PAUSE)

                new_urls_count = len(urls) - current_urls_on_page_load
                logger.info(f"Page {pnum}: +{new_urls_count} URLs. Total: {len(urls)}")

                # Pagination end detection
                if max_pages == -1 and new_urls_count == 0 and pnum > 1:
                    logger.info("End of pagination detected (no new products)")
                    return urls
                
                # Check for anti-scraping measures here (e.g., CAPTCHA selector)
                # if await page.query_selector(".captcha-element"):
                #     logger.critical("CAPTCHA DETECTED. Stopping URL collection.")
                #     return urls

                break # Page loaded successfully

            except PlaywrightTimeoutError as e:
                logger.warning(f"Timeout on index page {pnum} (attempt {attempt}).")
                await asyncio.sleep(1.5 ** attempt) # Exponential backoff
            except Exception as e:
                logger.error(f"Generic error on index page {pnum} (attempt {attempt}): {e}")
                await asyncio.sleep(1.5 ** attempt)

        pnum += 1

    return urls

async def scrape_single_product(page: Page, url: str, failed_log_data: list, stop_flag: asyncio.Event = None) -> Optional[ScrapedItem]:
    """
    Scrape a single product page, trying JSON-LD first and falling back to HTML.
    Returns a ScrapedItem dataclass instance.
    """
    BASE_BACKOFF: float = 1.5
    url_to_scrape: str = _sanitize_url(url)
    current_time: datetime = datetime.now()
    
    # Check for early stop
    if stop_flag and stop_flag.is_set():
        return None

    for attempt in range(1, MAX_PRODUCT_RETRIES + 1):
        try:
            # 1. Page Navigation
            await page.goto(url_to_scrape, wait_until="load", timeout=PAGE_LOAD_TIMEOUT)

            # Check for HTTP 403/429 status code for anti-bot detection
            if page.main_frame.url.startswith("data:text/html"):
                 logger.warning(f"Failed to navigate to {url_to_scrape}. Possible block or redirect.")
                 raise PlaywrightTimeoutError("Navigation blocked.")

            # Try different ways to wait for and find the JSON-LD script
            try:
                # First, wait for page to be fully loaded
                await page.wait_for_load_state('networkidle', timeout=10000)
                
                # Try JSON-LD parsing
                json_data = await parse_product_jsonld(page)
                
                if json_data is None:
                    logger.debug(f"Skipping non-{TARGET_BRAND} product: {url_to_scrape}")
                    return None

                # Get availability information
                availability_text_clean = "-1"
                try:
                    availability_selector = '.product__delivery .terms'
                    el = await page.query_selector(availability_selector)
                    if el:
                        availability_text = await el.inner_text()
                        availability_text_clean = availability_text
                except Exception as e:
                    logger.debug(f"Error extracting availability from DOM: {str(e)}")
                
                if is_valid_sku(json_data.get('sku_raw')):
                    logger.debug(f"Success: {TARGET_BRAND} product found for {url_to_scrape}")
                    return ScrapedItem(
                        sku_raw=json_data['sku_raw'],
                        price_raw=json_data['price_raw'],
                        price_old_raw=json_data['price_old_raw'],
                        availability_raw=availability_text_clean,
                        url=url_to_scrape,
                        timestamp=current_time,
                        detection_status="OK"
                    )
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout waiting for JSON-LD on {url_to_scrape}")
                # log_failed_urls(failed_log_data)
            except Exception as e:
                logger.warning(f"Error parsing JSON-LD on {url_to_scrape}: {str(e)}")
                # log_failed_urls(failed_log_data)

            # 3. Fallback to HTML scraping
            # If JSON-LD failed, log a warning and capture minimal fallback data
            
            # --- Anti-Scraping Check Placeholder ---
            # if await page.query_selector(".anti-bot-message"):
            #     logger.error(f"Anti-Bot message detected on {url_to_scrape}")
            #     failed_log_writer.writerow([url_to_scrape, "CAPTCHA/Bot Block", "N/A"])
            #     return ScrapedItem("N/A_failed", "-1", "Blocked", url_to_scrape, current_time, "CAPTCHA_DETECTED")
            # ----------------------------------------
            
            # Fallback to HTML scraping
            price_text: str = ""
            price_old_text: str = ""
            availability_text_clean: str = "-1"

            try:
                # Try to find the new price (using multiple possible selectors)
                price_selectors = [
                    ".price__new",  # Main new price
                    "span.price",    # Alternative selector
                    ".product-price" # Another alternative
                ]
                for selector in price_selectors:
                    el = await page.query_selector(selector)
                    if el:
                        price_text = await el.inner_text()
                        break
                
                # Try to find the old price
                old_price_selectors = [
                    ".price__old",      # Main old price
                    ".price--old",      # Alternative
                    ".original-price"   # Another alternative
                ]
                for selector in old_price_selectors:
                    el = await page.query_selector(selector)
                    if el:
                        price_old_text = await el.inner_text()
                        break

                # Try to get availability information
                availability_selector = '.product__delivery .terms'
                el = await page.query_selector(availability_selector)
                if el:
                    availability_text = await el.inner_text()
                    availability_text_clean = availability_text#availability_dict.get(availability_text, availability_text)

                # Clean up the prices (remove currency symbols and spaces)
                price_text = re.sub(r'[^\d.]', '', price_text)
                if price_old_text:
                    price_old_text = re.sub(r'[^\d.]', '', price_old_text)

            except Exception as e:
                logger.debug(f"HTML extraction failed for {url_to_scrape}: {str(e)}")

            # Only log as failure if we couldn't get the current price
            if not price_text:
                failed_log_data.append([
                    url_to_scrape,
                    "No price found in either JSON-LD or HTML",
                    f"Price: Not found, Old Price: {price_old_text or 'Not found'}, Availability: {availability_text_clean}"
                ])

            return ScrapedItem(
                sku_raw=get_product_id_from_url(url_to_scrape) or "N/A_fallback",
                price_raw=price_text or "-1",
                price_old_raw=price_old_text or "-1",
                availability_raw=availability_text_clean,
                url=url_to_scrape,
                timestamp=current_time,
                detection_status="OK" # OK means page was reachable, failure was in parsing
            )

        except PlaywrightTimeoutError:
            logger.warning(f"Timeout {url_to_scrape} (attempt {attempt})")
            await asyncio.sleep(BASE_BACKOFF ** attempt * 0.5)
            continue
        except Exception as e:
            logger.error(f"Error {url_to_scrape} (attempt {attempt}): {e}")
            await asyncio.sleep(BASE_BACKOFF ** attempt * 0.5)
            continue

    # Final failure after all retries
    failed_log_data.append([url_to_scrape, "Failed after all retries", "N/A"])
    logger.warning(f"Giving up on {url_to_scrape}")
    return ScrapedItem("N/A_failed", "-1", "Failed", url_to_scrape, current_time, "HTTP_BLOCKED")