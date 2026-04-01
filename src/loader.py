import logging
from turtle import pd
from typing import List,Iterable
from pathlib import Path
import duckdb
from itertools import islice

from generic_config import DB_FILENAME
from src.models import FinalProductRecord

logger = logging.getLogger(__name__)

def get_latest_prices(db_path: str = "data/prices.duckdb") -> dict:
    """
    Fetches the most recent price for each normalized_sku from the price_history table.
    Returns a dictionary mapping sku to its latest price.
    """
    if not Path(db_path).exists():
        logger.warning(f"Database {db_path} does not exist. No previous prices found.")
        return {}

    try:
        with duckdb.connect(db_path) as con:
            # Query for the latest price for each SKU
            # We use a subquery to find the max timestamp per SKU
            query = """
                SELECT normalized_sku, price
                FROM price_history
                WHERE (normalized_sku, timestamp) IN (
                    SELECT normalized_sku, MAX(timestamp)
                    FROM price_history
                    GROUP BY normalized_sku
                )
            """
            rows = con.execute(query).fetchall()
            return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.error(f"Error fetching latest prices from {db_path}: {e}")
        return {}

def insert_data(records: List[FinalProductRecord], db_path: str = "prices.duckdb"):
    if not records:
        return 0

    with duckdb.connect(db_path) as con:
        # 2. Schema with Timestamp (Essential for price tracking)
        con.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                normalized_sku VARCHAR,
                collection VARCHAR,
                price DOUBLE,
                price_old DOUBLE,
                price_promo DOUBLE,
                availability_code VARCHAR,
                url VARCHAR,
                detection_status VARCHAR,
                timestamp TIMESTAMP DEFAULT date_trunc('hour', CURRENT_TIMESTAMP) 
            )
        """)
        # 1. Ensure the tuple contains ALL data defined in the schema (except the auto-timestamp)
        data = (
            (
                r.normalized_sku, 
                r.collection, 
                float(r.price), 
                float(r.price_old), 
                float(r.price_promo), 
                str(r.availability_code), 
                str(r.url), 
                r.detection_status
            ) 
            for r in records
        )

        # 2. Match the column names in the INSERT to the CREATE TABLE names
        con.executemany("""
            INSERT INTO price_history (
                normalized_sku, 
                collection, 
                price, 
                price_old, 
                price_promo, 
                availability_code, 
                url, 
                detection_status
            ) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, data)
        
        count = len(records)
        logger.info(f"Successfully appended {count} records to price_history.")
        return count