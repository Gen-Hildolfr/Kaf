import sqlite3
import yfinance as yf

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
        ("Bibit", "SMMF", 3278.998, "IDR"),
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


def get_investment_totals(db_name: str = DB_NAME) -> dict:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute("SELECT platform, ticker, units, currency FROM holdings")
    rows = cursor.fetchall()
    conn.close()

    totals = {}
    for platform, ticker, units, currency in rows:
        if platform == "Gotrade" or ticker == "VTI":
            ticker_obj = yf.Ticker("VTI")
            try:
                hist = ticker_obj.history(period="1d")
                if not hist.empty and "Close" in hist:
                    price = float(hist["Close"].iloc[-1])
                else:
                    price = float(ticker_obj.fast_info.last_price)
            except Exception:
                price = float(ticker_obj.fast_info.last_price)
            total_usd = units * price
            totals[platform] = totals.get(platform, 0.0) + total_usd
        elif platform == "Bibit" or ticker == "SMMF":
            smmf_nav = 1991.55
            total_idr = units * smmf_nav
            totals[platform] = totals.get(platform, 0.0) + total_idr

    return totals


if __name__ == "__main__":
    init_db()

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE holdings SET units = 3278.998 WHERE ticker = 'SMMF'")
    conn.commit()

    print("--- ACCOUNTS ---")
    cursor.execute("SELECT platform, balance, currency, updated_at FROM accounts")
    for row in cursor.fetchall():
        print(row)

    print("\n--- HOLDINGS ---")
    cursor.execute("SELECT id, platform, ticker, units, currency FROM holdings")
    for row in cursor.fetchall():
        print(row)

    conn.close()

    print("\n--- INVESTMENT TOTALS ---")
    totals = get_investment_totals()
    print(totals)
    for platform, total in totals.items():
        if platform == "Gotrade":
            print(f"{platform} (VTI): ${total:.2f} USD")
        elif platform == "Bibit":
            print(f"{platform} (SMMF): Rp {total:,.2f} IDR (~Rp {total / 1_000_000:.2f}M IDR)")
