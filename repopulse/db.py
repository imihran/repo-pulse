"""
Database connection factory.
Reads credentials from environment variables (populated by python-dotenv from .env).
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Use an absolute path so this works regardless of working directory
# (e.g. when called from an Airflow task vs. the project root CLI).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def get_connection() -> psycopg.Connection:
    """
    Open and return a psycopg3 connection.
    Caller is responsible for closing it (or use as a context manager).
    """
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5433)),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        dbname=os.getenv("POSTGRES_DB"),
    )
