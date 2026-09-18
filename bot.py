import sqlite3

DB_NAME = "finance.db"


def init_db(db_name: str = DB_NAME) -> None:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            platform TEXT PRIMARY KEY,
            balance REAL DEFAULT 0,
            currency TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            ticker TEXT,
            units REAL DEFAULT 0,
            currency TEXT,
            UNIQUE(platform, ticker)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATETIME DEFAULT CURRENT_TIMESTAMP,
            platform TEXT,
            type TEXT,
            amount REAL,
            category TEXT,
            note TEXT
        )
    """)

    accounts_seed = [
        ("Cash", 0, "IDR"),
        ("BCA", 0, "IDR"),
        ("Jenius", 0, "IDR"),
        ("Bibit", 0, "IDR"),
        ("Gotrade", 0, "USD"),
    ]
    cursor.executemany(
        """
        INSERT OR IGNORE INTO accounts (platform, balance, currency)
        VALUES (?, ?, ?)
        """,
        accounts_seed,
    )

    holdings_seed = [
        ("Bibit", "SMMF", 32789.980, "IDR"),
        ("Gotrade", "VTI", 0.0902, "USD"),
    ]
    cursor.executemany(
        """
        INSERT OR IGNORE INTO holdings (platform, ticker, units, currency)
        VALUES (?, ?, ?, ?)
        """,
        holdings_seed,
    )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    print("--- ACCOUNTS ---")
    cursor.execute("SELECT platform, balance, currency, updated_at FROM accounts")
    for row in cursor.fetchall():
        print(row)

    print("\n--- HOLDINGS ---")
    cursor.execute("SELECT id, platform, ticker, units, currency FROM holdings")
    for row in cursor.fetchall():
        print(row)

    conn.close()
