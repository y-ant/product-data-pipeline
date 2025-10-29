import sqlite3
import logging
from typing import List
from pathlib import Path

from generic_config import DB_FILENAME, DB_TABLE_NAME
from src.models import FinalProductRecord

logger = logging.getLogger(__name__)

def setup_database(db_path: Path = DB_FILENAME, table_name: str = DB_TABLE_NAME) -> None:
    """Sets up the SQLite database and the product table."""
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create table if it doesn't exist
        # We use normalized_sku as a primary key to ensure no duplicates.
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                normalized_sku TEXT PRIMARY KEY,
                price REAL NOT NULL,
                price_old REAL DEFAULT 0,
                availability_code TEXT,
                url TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                detection_status TEXT
            );
        """)
        conn.commit()
        logger.info(f"Database {db_path} and table {table_name} ensured.")
    except sqlite3.Error as e:
        logger.error(f"SQLite error during setup: {e}")
    finally:
        if conn:
            conn.close()

def insert_data(records: List[FinalProductRecord], db_path: Path = DB_FILENAME, table_name: str = DB_TABLE_NAME) -> int:
    """
    Inserts or updates (UPSERT) product records into the database.
    Uses REPLACE INTO to handle existing SKUs (taking the latest scrape).
    """
    conn = None
    insertion_count: int = 0
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        sql = f"""
            REPLACE INTO {table_name} 
            (normalized_sku, price, price_old, availability_code, url, timestamp, detection_status) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        
        data_to_insert = [
            (
                r.normalized_sku, 
                r.price,
                r.price_old,
                r.availability_code, 
                r.url, 
                r.timestamp.isoformat(), 
                r.detection_status
            )
            for r in records
        ]
        
        cursor.executemany(sql, data_to_insert)
        conn.commit()
        insertion_count = cursor.rowcount
        logger.info(f"Successfully UPSERTED {insertion_count} records into {table_name}.")
        return insertion_count
        
    except sqlite3.Error as e:
        logger.error(f"SQLite error during data insertion: {e}")
        return 0
    finally:
        if conn:
            conn.close()