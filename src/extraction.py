import re
import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Set, Optional, Dict

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from generic_config import (
    PAGE_LOAD_TIMEOUT, MAX_PRODUCT_RETRIES, BLOCK_RESOURCES, BLOCKED_DOMAINS,
    BASE_URL, BRAND_PAGE
)
from src.models import ScrapedItem, FinalProductRecord, DetectionStatus, NormalizedData

logger = logging.getLogger(__name__)

# --- Constants ---
EXCLUDE_FRAGMENTS = ['policy', 'terms', 'privacy', 'cookie', 'contact', 'about', 'faq', 'help']
PRODUCT_LINK_SELECTOR = "a[href*='/p']"
SCROLL_ATTEMPTS = 5
SCROLL_PAUSE = 1.0
MAX_PAGE_RETRIES = 3


class SchemaNotFoundError(Exception):
    """Raised when JSON-LD schema is not found after retries."""
    pass

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

def _clean_price(raw: str) -> str:
    """Strip non-numeric chars from a raw price string. Returns '-1' on empty input."""
    if not raw or raw == "-1":
        return "-1"
    cleaned = re.sub(r'[^\d.]', '', raw)
    return cleaned if cleaned else "-1"

def normalize_price(price_raw: str) -> float:
    """
    Cleans raw price string and converts it to a float.
    Returns -1.0 if conversion fails.
    """
    try:
        if isinstance(price_raw, str):
            cleaned = _clean_price(price_raw)
            return float(cleaned)
        return float(price_raw)
    except (ValueError, TypeError):
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
            collection=item.collection,
            price_old=normalize_price(item.price_old_raw),
            price_promo=normalize_price(item.price_promo_raw),
            availability_code=item.availability_raw,
            url=item.url,
            detection_status=item.detection_status
        )
        filtered_records.append(record)

    return filtered_records

# --- Playwright Helpers (E-Step) ---

async def _block_resources(page: Page) -> None:
    """Enhanced resource blocking with performance optimization."""
    async def route_handler(route):
        try:
            request_url = route.request.url
            resource_type = route.request.resource_type
            
            # Allow essential resources immediately
            essential_types = {'document', 'script', 'xhr', 'fetch'}
            if resource_type in essential_types:
                await route.continue_()
                return

            # Block known advertising and tracking domains immediately
            ad_tracking_keywords = [
                'google-analytics', 'googleads', 'doubleclick',
                'facebook', 'google-tag', 'yandex', 'metrika'
            ]
            if any(keyword in request_url.lower() for keyword in ad_tracking_keywords):
                await route.abort()
                return
                
            # Block by resource type
            if resource_type in BLOCK_RESOURCES:
                await route.abort()
                return
                
            # Block by domain using configuration
            if any(domain.replace('*', '') in request_url for domain in BLOCKED_DOMAINS):
                await route.abort()
                return
                
            # Block heavy media resources
            if resource_type in {'image', 'media', 'font'}:
                # Allow small images and essential fonts
                if resource_type == 'image' and '.svg' in request_url.lower():
                    await route.continue_()
                    return
                    
                headers = route.request.headers
                if 'accept' in headers and 'image/svg' in headers['accept']:
                    await route.continue_()
                    return
                    
                await route.abort()
                return
            
            # Continue with everything else
            await route.continue_()
            
        except Exception as e:
            logger.error(f"Error in route handler: {str(e)}")
            # On error, continue the request to prevent hanging
            await route.continue_()
    
    # Set up routes with error handling
    try:
        await page.route("**/*", route_handler)
    except Exception as e:
        logger.error(f"Failed to set up resource blocking: {str(e)}")
        
    # Set timeouts at page level
    page.set_default_timeout(PAGE_LOAD_TIMEOUT)  # For navigations
    page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)  # Specific for navigation
    
    # Optimize page performance
    await page.set_extra_http_headers({
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })

async def extract_sku_from_page_title(page: Page) -> Optional[str]:
    """Helper to extract a 6-15 digit SKU from the page title."""
    try:
        page_title = await page.title()
        matches = re.finditer(r'(?<!\d)\d(?:[\- ]?\d){5,14}(?!\d)', page_title)
        for m in matches:
            clean_candidate = m.group(0).replace('-', '').replace(' ', '')
            if 6 <= len(clean_candidate) <= 15:
                return clean_candidate
    except Exception as e:
        logger.debug(f"Error extracting SKU from title: {str(e)}")
    return None

async def parse_html_data(page: Page) -> Dict[str, str]:
    """Extracts UI pricing, collection, and availability strictly manually via DOM query_selectors."""
    dom_data = {
        "price_promo_raw": "-1",
        "price_raw": "",
        "price_old_raw": "-1",
        "collection": "General",
        "availability_raw": "-1"
    }

    try:
        # Try to find PROMO price first
        promo_el = await page.query_selector(".active-coupon__price")
        if promo_el:
            dom_data["price_promo_raw"] = await promo_el.inner_text()
            
        # Try to find the new price
        price_selectors = [".price__new", "span.price", ".product-price"]
        for selector in price_selectors:
            el = await page.query_selector(selector)
            if el:
                dom_data["price_raw"] = await el.inner_text()
                break
        
        # Try to find the old price
        old_price_selectors = [".price__old", ".price--old", ".original-price"]
        for selector in old_price_selectors:
            el = await page.query_selector(selector)
            if el:
                dom_data["price_old_raw"] = await el.inner_text()
                break

        # Try to get collection info
        try:
            breadcrumb_links = await page.query_selector_all(".breadcrumb__link")
            if len(breadcrumb_links) > 1:
                collection_text = await breadcrumb_links[-2].inner_text()
                dom_data["collection"] = collection_text.strip()
            
            if dom_data["collection"] == "General":
                char_selectors = ["li:has-text('Колекція')", "li:has-text('Коллекция')"]
                for selector in char_selectors:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        val_el = await el.query_selector("span:last-child")
                        if val_el:
                            text = await val_el.inner_text()
                            if text and text.strip():
                                dom_data["collection"] = text.strip()
                                break
                    if dom_data["collection"] != "General":
                        break
        except Exception as e:
            logger.debug(f"Collection extraction failed: {e}")

        # Try to get availability information
        availability_selectors = [
            "section.article.product-detail .item-column .terms",
            "xpath=//div[contains(@class, 'product__delivery')]//*[contains(text(), 'Відправка') or contains(text(), 'В наявності')]",
            ".product-info__delivery .terms"
        ]
        for selector in availability_selectors:
            el = await page.query_selector(selector)
            if el:
                dom_data["availability_raw"] = await el.inner_text()
                break
                
        # Clean price texts
        dom_data["price_raw"] = _clean_price(dom_data["price_raw"])
        dom_data["price_old_raw"] = _clean_price(dom_data["price_old_raw"])
        dom_data["price_promo_raw"] = _clean_price(dom_data["price_promo_raw"])
        
    except Exception as e:
        logger.debug(f"HTML extraction failed during DOM parse: {str(e)}")
        
    return dom_data

async def parse_product_jsonld(page: Page, title_sku: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Extracts product SKU and price from JSON-LD structured data.

    Scans the page for 'application/ld+json' scripts to find Schema.org
    Product data. Retries up to 3 times with exponential backoff if the
    script tags haven't rendered yet.

    Args:
        page: The Playwright Page instance to scrape.
        title_sku: Pre-extracted SKU from the page title (takes priority).

    Returns:
        A dict with 'sku_raw' and 'price_raw', or None if no product found.
    """
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(SchemaNotFoundError),
        reraise=True
    )
    async def get_product_schema_robustly(page):
        """Query JSON-LD scripts, retrying if not yet available."""
        scripts = await page.query_selector_all('script[type="application/ld+json"]')
        if not scripts:
            raise SchemaNotFoundError("JSON-LD script list was empty.")
        return scripts

    all_scripts = []
    try:
        all_scripts = await get_product_schema_robustly(page)
    except SchemaNotFoundError:
        logger.warning("No JSON-LD found on page after 3 retries.")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching JSON-LD scripts: {e}")
        return None

    for s in all_scripts:
        try:
            text = await s.evaluate('el => el.innerHTML || el.innerText')
            text = text.strip()
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                try:
                    data = json.loads(clean_json_string(text))
                except Exception as inner_e:
                    logger.debug(f"JSON deep decode error: {inner_e}")
                    continue

            candidates = data if isinstance(data, list) else [data]

            for item in candidates:
                if not isinstance(item, dict) or item.get("@type") != "Product":
                    continue

                sku = title_sku
                if not sku:
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

                price = _clean_price(str(offers.get("price", "-1")).strip())

                logger.debug(f"Found JSON-LD data: SKU={sku}, Price={price}")
                return {"sku_raw": sku, "price_raw": price}

        except json.JSONDecodeError as e:
            logger.debug(f"JSON decode error: {e}")
        except Exception as e:
            logger.debug(f"Error parsing JSON-LD: {e}")

    return None

# --- Main Scraping Logic (E-Step) ---

# TODO: Not currently called from run_cli.py — kept for future use (URL discovery from brand pages)
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
                try:
                    # First try with networkidle
                    await page.goto(
                        page_url,
                        wait_until="networkidle",
                        timeout=PAGE_LOAD_TIMEOUT // 2  # Use shorter timeout for first attempt
                    )
                except PlaywrightTimeoutError:
                    logger.info("Network idle timeout, falling back to domcontentloaded...")
                    # Fall back to domcontentloaded if networkidle times out
                    await page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT
                    )
                    
                # Check if we need to wait for specific elements
                try:
                    await page.wait_for_selector(PRODUCT_LINK_SELECTOR, timeout=5000)
                except PlaywrightTimeoutError:
                    logger.warning("Product links not immediately available, continuing anyway")
                
                # Add small delay for dynamic content
                await page.wait_for_timeout(1000)

                prev_count: int = -1
                current_urls_on_page_load: int = len(urls)

                for _ in range(SCROLL_ATTEMPTS):
                    anchors = await page.query_selector_all(".product__item a[href*='/p']")

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

                break # Page loaded successfully

            except PlaywrightTimeoutError as e:
                logger.warning(f"Timeout on index page {pnum} (attempt {attempt}).")
                await asyncio.sleep(1.5 ** attempt) # Exponential backoff
            except Exception as e:
                logger.error(f"Generic error on index page {pnum} (attempt {attempt}): {e}")
                await asyncio.sleep(1.5 ** attempt)

        pnum += 1

    return urls


async def _check_access(page: Page) -> bool:
    """Returns True if the page shows an access denied / blocked message."""
    page_html = await page.content()
    page_text_lower = page_html.lower()
    if "access denied" in page_text_lower or "blocked" in page_text_lower:
        logger.warning("Blocked or Access Denied page detected")
        return True
    return False


async def scrape_single_product(page: Page, url: str, failed_log_data: list, stop_flag: asyncio.Event = None) -> Optional[ScrapedItem]:
    """
    Scrape a single product page, trying JSON-LD first and falling back to HTML.
    Returns a ScrapedItem dataclass instance.
    """
    BASE_BACKOFF: float = 1.5
    url_to_scrape: str = _sanitize_url(url)
    
    # Check for early stop
    if stop_flag and stop_flag.is_set():
        return None

    for attempt in range(1, MAX_PRODUCT_RETRIES + 1):
        try:
            # Add increasing delay between retries
            if attempt > 1:
                delay = BASE_BACKOFF ** (attempt - 1) * 2
                logger.info(f"Retry attempt {attempt} for {url_to_scrape}, waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                
                if attempt > MAX_PRODUCT_RETRIES - 1:
                    logger.info(f"Last retry attempt, clearing cookies for {url_to_scrape}")
                    await page.context.clear_cookies()
            
            # 1. Page Navigation
            logger.info(f"Attempting to navigate to {url_to_scrape}")
            try:
                response = await page.goto(
                    url_to_scrape, 
                    wait_until="domcontentloaded", 
                    timeout=PAGE_LOAD_TIMEOUT * 2
                )
                logger.info(f"Basic page load completed for {url_to_scrape}")
                
                status = response.status if response else 'No response'
                logger.info(f"Response status for {url_to_scrape}: {status}")
                
                if response and not response.ok:
                    logger.error(f"Bad response status {status} for {url_to_scrape}")

            except PlaywrightTimeoutError as e:
                logger.warning(f"Initial page load timeout for {url_to_scrape}: {e}")
                raise
            except Exception as e:
                logger.error(f"Navigation error for {url_to_scrape}: {type(e).__name__}: {e}")
                raise

            if page.main_frame.url.startswith("data:text/html"):
                logger.warning(f"Failed to navigate to {url_to_scrape}. Possible block or redirect.")
                raise PlaywrightTimeoutError("Navigation blocked.")

            if await _check_access(page):
                logger.error(f"Access denied/blocked on {url_to_scrape}")
                raise Exception("Access denied")
                
            try:
                # 1. First, attempt to extract the SKU from the title
                title_sku = await extract_sku_from_page_title(page)
                
                # 2. Try JSON-LD parsing
                logger.info(f"Attempting to parse JSON-LD for {url_to_scrape}")
                json_data = await parse_product_jsonld(page, title_sku=title_sku) or {}
                
                # 3. Always run HTML scraper for collection/availability/promos which JSON-LD lacks
                logger.info(f"Extracting HTML DOM details for {url_to_scrape}")
                html_data = await parse_html_data(page)

                # 4. Merge Data prioritizing JSON-LD
                final_sku = json_data.get('sku_raw') or title_sku or get_product_id_from_url(url_to_scrape) or "N/A_fallback"
                final_price = json_data.get('price_raw') or html_data.get('price_raw', '-1')
                
                if json_data and not json_data.get('price_raw'):
                    logger.debug(f"No JSON-LD price for {url_to_scrape}. Using HTML fallback price.")
                    
                if not json_data and final_price == "-1":
                    failed_log_data.append([
                        url_to_scrape,
                        "No price found in either JSON-LD or HTML",
                        f"Price: Not found, Old Price: {html_data.get('price_old_raw', '-1')}, Availability: {html_data.get('availability_raw', '-1')}"
                    ])

                if is_valid_sku(final_sku):
                    logger.debug(f"Success: product found for {url_to_scrape}")
                    return ScrapedItem(
                        sku_raw=final_sku,
                        collection=html_data['collection'],
                        price_raw=final_price,
                        price_old_raw=html_data['price_old_raw'],
                        price_promo_raw=html_data['price_promo_raw'],
                        availability_raw=html_data['availability_raw'],
                        url=url_to_scrape,
                        detection_status="OK"
                    )
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout parsing data on {url_to_scrape}")
            except Exception as e:
                logger.warning(f"Error processing data on {url_to_scrape}: {e}")

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
    return ScrapedItem(
        sku_raw="N/A_failed",
        collection="General",
        price_raw=-1.0,
        availability_raw="Failed",
        url=url_to_scrape,
        detection_status="HTTP_BLOCKED"
    )

def clean_json_string(raw_json_text: str) -> str:
    """
    Cleans up raw JSON text by escaping common invalid control characters 
    before decoding.
    """
    # 1. Remove all unescaped newline, carriage return, and tab characters.
    # The 're.sub' function replaces the pattern with the proper escaped character.
    # Newlines (\n) and carriage returns (\r) are the most common offenders.
    
    # Escape newlines (\n) that are not already escaped (e.g., \\n)
    cleaned_text = re.sub(r'(?<!\\)[\n\r]', ' ', raw_json_text)
    cleaned_text = re.sub(r'[\t]+', ' ', cleaned_text)
    
    # NOTE: You may need more advanced cleaning if the issue is unescaped double quotes 
    # within nested strings (which is a harder problem that might require a specialized parser).
    
    return cleaned_text