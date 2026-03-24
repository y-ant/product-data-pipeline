import logging
from turtle import pd
from typing import List,Iterable
from pathlib import Path
import duckdb
from itertools import islice

from generic_config import DB_FILENAME
from src.models import FinalProductRecord

logger = logging.getLogger(__name__)

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