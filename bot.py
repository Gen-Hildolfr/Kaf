import io
import os
import sys
import re
import glob
import json
import base64
import asyncio
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
import traceback
import requests
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
log_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

file_handler = RotatingFileHandler(
    "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(file_handler)
root_logger.addHandler(stream_handler)

logger = logging.getLogger("kaf")
logging.getLogger("discord.client").setLevel(logging.ERROR)

# ---------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
UNSLOTH_API_URL = os.getenv("UNSLOTH_API_URL", "http://localhost:8000/v1/chat/completions")
UNSLOTH_API_KEY = os.getenv("UNSLOTH_API_KEY", "")
UNSLOTH_MODEL = os.getenv("UNSLOTH_MODEL", "ukisai/Swift-Qwen3.8-27B-GGUF")
USER_NAME = os.getenv("USER_NAME", "")
BCA_ACCOUNT_NO = os.getenv("BCA_ACCOUNT_NO", "")
JENIUS_ACCOUNT_NO = os.getenv("JENIUS_ACCOUNT_NO", "")
RDN_ACCOUNT_NO = os.getenv("RDN_ACCOUNT_NO", "")
DB_NAME = "finance.db"
BACKUP_DIR = "backups"
NAV_CACHE_FILE = "nav_cache.json"
WIB = timezone(timedelta(hours=7))
SEED_SMMF_NAV = 1991.55
PASARDANA_AUTH = os.getenv("PASARDANA_AUTH", "")

PERSONA_PATH = os.path.join(os.path.dirname(__file__), "persona.md")
if os.path.exists(PERSONA_PATH):
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        SYSTEM_PERSONA = f.read().strip()
else:
    SYSTEM_PERSONA = "You are Kaf, a meticulous, supportive, and slightly teasing maid managing personal finances."

PLATFORM_MAP = {
    "cash": "Cash",
    "bca": "BCA",
    "jenius": "Jenius",
    "bibit": "Bibit",
    "gotrade": "Gotrade",
}

# ---------------------------------------------------------
# Database Initialization & Helpers
# ---------------------------------------------------------
def init_db(db_name: str = DB_NAME) -> None:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")

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
        ("Bibit", "SMMF", 0.0, "IDR"),
        ("Gotrade", "VTI", 0.0, "USD"),
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
    logger.info(f"Initialized SQLite database '{db_name}' in WAL mode.")


def perform_db_backup(db_name: str = DB_NAME) -> str:
    """Safely creates an atomic SQLite backup using the backup API."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"finance_backup_{timestamp}.db")

    src = sqlite3.connect(db_name)
    dst = sqlite3.connect(backup_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    logger.info(f"Created atomic SQLite backup for '{db_name}' at: {backup_path}")
    return backup_path


def check_and_run_biweekly_backup(db_name: str = DB_NAME) -> str | None:
    """Runs a backup if the last backup is older than 14 days or doesn't exist."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    existing_backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "finance_backup_*.db")))

    needs_backup = True
    if existing_backups:
        latest_backup = existing_backups[-1]
        mtime = datetime.fromtimestamp(os.path.getmtime(latest_backup))
        if datetime.now() - mtime < timedelta(days=14):
            needs_backup = False

    if needs_backup:
        logger.info(f"Biweekly backup check: triggering backup for '{db_name}'")
        return perform_db_backup(db_name)
    return None


def get_most_recent_friday_21_wib(now_dt: datetime) -> datetime:
    """Calculates the datetime of the most recent Friday at 21:00 WIB (UTC+7)."""
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=WIB)
    else:
        now_dt = now_dt.astimezone(WIB)

    weekday = now_dt.weekday()  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if weekday == 4:
        if now_dt.hour >= 21:
            days_ago = 0
        else:
            days_ago = 7
    elif weekday > 4:
        days_ago = weekday - 4
    else:
        days_ago = weekday + 3

    target_date = now_dt.date() - timedelta(days=days_ago)
    return datetime(target_date.year, target_date.month, target_date.day, 21, 0, 0, tzinfo=WIB)


def fetch_smmf_nav(force_refresh: bool = False) -> float:
    """Dynamic Friday-aligned SMMF NAV fetcher.

    Checks nav_cache.json. If updated_at >= most recent Friday 21:00 WIB and not force_refresh,
    returns cached NAV without making network requests.
    Otherwise queries Bareksa endpoint, with fallbacks to Pasardana, Bibit, dynamic cache, and SEED_SMMF_NAV.
    """
    now_wib = datetime.now(WIB)
    most_recent_friday = get_most_recent_friday_21_wib(now_wib)

    cached_nav = None
    if os.path.exists(NAV_CACHE_FILE):
        try:
            with open(NAV_CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                cached_nav = float(cache_data.get("nav", 0.0))
                updated_at_str = cache_data.get("updated_at", "")
                if updated_at_str:
                    cached_dt = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
                    if not force_refresh and cached_dt >= most_recent_friday and cached_nav > 0:
                        logger.info(f"Using Friday-aligned cached SMMF NAV: {cached_nav}")
                        return cached_nav
        except Exception as e:
            logger.warning(f"Failed to read {NAV_CACHE_FILE}: {e}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    nav = None

    # Primary: Bareksa
    try:
        bareksa_url = "https://www.bareksa.com/id/data/reksadana/1742/sucorinvest-money-market-fund"
        r = requests.get(bareksa_url, headers=headers, timeout=10)
        if r.status_code == 200:
            match = re.search(r'<span class="fS40">([\d\.,]+)</span>', r.text)
            if match:
                val = float(match.group(1).replace(".", "").replace(",", "."))
                if 1500.0 < val < 3000.0:
                    nav = val
    except Exception:
        pass

    if nav is None:
        try:
            bareksa_api = "https://www.bareksa.com/api/invest/product/detail/sucorinvest-money-market-fund"
            r = requests.get(bareksa_api, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                val = data.get("data", {}).get("nav") or data.get("nav")
                if val and 1500.0 < float(val) < 3000.0:
                    nav = float(val)
        except Exception:
            pass

    # Secondary Fallback: Pasardana
    if nav is None:
        try:
            pasardana_headers = headers.copy()
            if PASARDANA_AUTH:
                pasardana_headers["Authorization"] = PASARDANA_AUTH
            pasardana_api = "https://pasardana.id/api/FundService/GetSnapshot?fundId=2058"
            r = requests.get(pasardana_api, headers=pasardana_headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                val = float(data.get("MaximumNavValue", 0.0))
                if 1500.0 < val < 3000.0:
                    nav = val
        except Exception:
            pass

    if nav is None:
        try:
            pasardana_url = "https://pasardana.id/fund/sucorinvest-money-market-fund"
            r = requests.get(pasardana_url, headers=headers, timeout=10)
            if r.status_code == 200:
                match = re.search(r'(?:NAV|NAB|Harga|Price)[\s\:\<\>\w\/\"\'\=]*?([\d\.,]{4,8})', r.text, re.IGNORECASE)
                if match:
                    val_str = match.group(1).replace(".", "").replace(",", ".")
                    val = float(val_str)
                    if 1500.0 < val < 3000.0:
                        nav = val
        except Exception:
            pass

    # Live Endpoint Fallback: Bibit (retrieves real-time SMMF NAV)
    if nav is None:
        try:
            bibit_url = "https://bibit.id/reksadana/RD1657/sucorinvest-money-market-fund"
            r = requests.get(bibit_url, headers=headers, timeout=10)
            if r.status_code == 200:
                candidates = re.findall(r'Rp<!-- -->([\d\.,]+)', r.text)
                for cand in candidates:
                    try:
                        cand_clean = cand.replace(",", "")
                        cand_val = float(cand_clean)
                        if 1500.0 < cand_val < 3000.0:
                            nav = cand_val
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    # If a live NAV was fetched from any network source, update cache file
    if nav is not None and nav > 0:
        try:
            cache_content = {
                "nav": nav,
                "updated_at": now_wib.strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(NAV_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_content, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write {NAV_CACHE_FILE}: {e}")
        logger.info(f"Retrieved live SMMF NAV: {nav}")
        return nav

    # Network Failure Fallback:
    # Use dynamically cached NAV from previous successful fetches so fallback tracks the latest known NAV.
    # Fall back to initial SEED_SMMF_NAV strictly if no cache file exists yet.
    fallback_result = cached_nav if (cached_nav and cached_nav > 0) else SEED_SMMF_NAV
    logger.info(f"Using fallback SMMF NAV: {fallback_result}")
    return fallback_result


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
                try:
                    price = float(ticker_obj.fast_info.last_price)
                except Exception:
                    price = 0.0  # Fallback to prevent command crash
            total_usd = units * price
            totals[platform] = totals.get(platform, 0.0) + total_usd
        elif platform == "Bibit" or ticker == "SMMF":
            smmf_nav = fetch_smmf_nav()
            total_idr = units * smmf_nav
            totals[platform] = totals.get(platform, 0.0) + total_idr

    return totals


def get_balance_breakdown(db_name: str = DB_NAME) -> dict:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT platform, balance, currency FROM accounts WHERE platform IN ('Cash', 'BCA', 'Jenius')"
    )
    account_rows = cursor.fetchall()

    cursor.execute("SELECT balance FROM accounts WHERE platform = 'Bibit'")
    bibit_row = cursor.fetchone()
    bibit_cash = float(bibit_row[0] or 0.0) if bibit_row and bibit_row[0] is not None else 0.0
    conn.close()

    liquid = {}
    for platform, balance, currency in account_rows:
        liquid[platform] = {
            "balance": float(balance),
            "currency": currency,
        }
    for p in ["Cash", "BCA", "Jenius"]:
        if p not in liquid:
            liquid[p] = {"balance": 0.0, "currency": "IDR"}

    investments = get_investment_totals(db_name=db_name)
    total_liquid_idr = sum(
        acc["balance"] for acc in liquid.values() if acc["currency"] == "IDR"
    )
    total_idr = total_liquid_idr + investments.get("Bibit", 0.0) + bibit_cash
    total_usd = investments.get("Gotrade", 0.0)

    return {
        "liquid": liquid,
        "investments": investments,
        "bibit_cash": bibit_cash,
        "total_liquid_idr": total_liquid_idr,
        "total_idr": total_idr,
        "total_usd": total_usd,
    }


def record_cash_flow(amount: float, tx_type: str, note: str, db_name: str = DB_NAME) -> float:
    amount = float(amount)
    delta = amount if tx_type.lower() == "income" else -amount

    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute("SELECT balance FROM accounts WHERE platform = 'Cash'")
    row = cursor.fetchone()
    current_balance = float(row[0]) if row and row[0] is not None else 0.0
    new_balance = current_balance + delta

    cursor.execute(
        """
        UPDATE accounts
        SET balance = ?, updated_at = CURRENT_TIMESTAMP
        WHERE platform = 'Cash'
        """,
        (new_balance,),
    )

    cursor.execute(
        """
        INSERT INTO transactions (platform, type, amount, category, note)
        VALUES ('Cash', ?, ?, 'Cash', ?)
        """,
        (tx_type, amount, note),
    )

    conn.commit()
    conn.close()
    return new_balance


def get_recent_activity(days: int = 7, db_name: str = DB_NAME) -> tuple[list, float, float]:
    """Retrieves recent transactions (expenses, outgoing transfers, and balance switching)
    in the last `days` days.

    Returns:
        rows: list of tuples (date, platform, type, category, amount, note)
        total_outflow: float (sum of expenses, fees, and external transfers; EXCLUDES balance switching)
        total_switched: float (sum of balance switching principal + RDN deposits)
    """
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT date, platform, type, category, amount, note
        FROM transactions
        WHERE date >= datetime('now', ?)
        ORDER BY date DESC
        """,
        (f"-{days} days",),
    )
    rows = cursor.fetchall()
    conn.close()

    total_outflow = 0.0
    total_switched = 0.0
    for r in rows:
        tx_type = r[2]
        category = r[3]
        amount = float(r[4])
        if tx_type == "transfer" and category in ("Switching", "Investment Funding"):
            total_switched += amount
        elif tx_type == "expense" or (tx_type == "transfer" and category not in ("Switching", "Investment Funding")):
            total_outflow += amount

    return rows, total_outflow, total_switched


def get_recent_outflows(days: int = 7, db_name: str = DB_NAME) -> tuple[list, float]:
    """Retrieves all outflows (direct expenses and outgoing transfers to external parties)

    in the last `days` days. Excludes internal balance switching (me to me).
    """
    rows, total_outflow, _ = get_recent_activity(days, db_name=db_name)
    outflow_rows = [r for r in rows if not (r[2] == "transfer" and r[3] in ("Switching", "Investment Funding"))]
    return outflow_rows, total_outflow


def get_recent_summary(tx_type: str, days: int = 7, db_name: str = DB_NAME) -> tuple[list, float]:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT date, platform, amount, note
        FROM transactions
        WHERE type = ? AND date >= datetime('now', ?)
        ORDER BY date DESC
        """,
        (tx_type, f"-{days} days"),
    )
    rows = cursor.fetchall()
    conn.close()

    total_amount = sum(float(r[2]) for r in rows) if rows else 0.0
    return rows, total_amount


def undo_last_transaction(db_name: str = DB_NAME) -> tuple[bool, str]:
    """Reverts the most recently recorded transaction in `transactions`.

    Reverses ledger effects based on type/category:
    - Expense: refunds amount (+ any admin fee) back to source account.
    - Income: deducts amount from source account.
    - Transfer (Switching / Investment Funding): refunds amount (+ fee) to source platform,
      and deducts amount from target platform.
    - Transfer (Outgoing): refunds amount (+ fee) to source platform.
    - Order / order_filled: reverts units in holdings and refunds amount in accounts.

    Deletes the record(s) from `transactions` and commits changes.

    Returns:
        tuple[bool, str]: (success, summary_message)
    """
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, platform, type, amount, category, note FROM transactions ORDER BY id DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "No transactions found to undo."

    tx_id, platform, tx_type, amount, category, note = row
    tx_type = (tx_type or "").lower()
    category = category or ""
    note = note or ""
    amount = float(amount or 0.0)

    fee = 0.0
    ids_to_delete = [tx_id]

    # Check if the latest transaction was an admin/transfer fee
    if category == "Fee" or note.startswith("Admin fee:") or note.startswith("Transfer fee for:"):
        fee_id = tx_id
        fee = amount
        cursor.execute(
            """
            SELECT id, platform, type, amount, category, note
            FROM transactions
            WHERE id < ? AND platform = ?
            ORDER BY id DESC LIMIT 1
            """,
            (fee_id, platform),
        )
        parent_row = cursor.fetchone()
        if parent_row:
            p_id, platform, tx_type, amount, category, note = parent_row
            tx_type = (tx_type or "").lower()
            category = category or ""
            note = note or ""
            amount = float(amount or 0.0)
            ids_to_delete = [p_id, fee_id]
    else:
        # Check if preceding or subsequent transaction was an associated fee
        cursor.execute(
            """
            SELECT id, amount FROM transactions
            WHERE id IN (?, ?) AND category = 'Fee' AND platform = ?
            """,
            (tx_id - 1, tx_id + 1, platform),
        )
        fee_row = cursor.fetchone()
        if fee_row:
            fee_id, fee_amt = fee_row
            fee = float(fee_amt or 0.0)
            ids_to_delete.append(fee_id)

    # 1. Revert Expense
    if tx_type == "expense":
        refund_amount = amount + fee
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        acc_row = cursor.fetchone()
        curr_bal = float(acc_row[0]) if acc_row and acc_row[0] is not None else 0.0
        currency = acc_row[1] if acc_row and acc_row[1] else "IDR"
        new_bal = curr_bal + refund_amount
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_bal, platform),
        )
        fee_text = f" (including Rp {fee:,.2f} fee)" if fee > 0 else ""
        summary_msg = (
            f"Reverted expense of Rp {amount:,.2f}{fee_text} on **{platform}**.\n"
            f"• Restored **{platform}** balance: Rp {new_bal:,.2f} {currency}"
        )

    # 2. Revert Income
    elif tx_type == "income":
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        acc_row = cursor.fetchone()
        curr_bal = float(acc_row[0]) if acc_row and acc_row[0] is not None else 0.0
        currency = acc_row[1] if acc_row and acc_row[1] else "IDR"
        new_bal = curr_bal - amount
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_bal, platform),
        )
        summary_msg = (
            f"Reverted income of Rp {amount:,.2f} on **{platform}**.\n"
            f"• Updated **{platform}** balance: Rp {new_bal:,.2f} {currency}"
        )

    # 3. Revert Transfer (Switching, Investment Funding, Outgoing)
    elif tx_type == "transfer":
        refund_src = amount + fee
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        src_row = cursor.fetchone()
        curr_src_bal = float(src_row[0]) if src_row and src_row[0] is not None else 0.0
        currency = src_row[1] if src_row and src_row[1] else "IDR"
        new_src_bal = curr_src_bal + refund_src
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_src_bal, platform),
        )

        target_platform = None
        if category in ("Switching", "Investment Funding"):
            note_lower = note.lower()
            if "bibit" in note_lower or (RDN_ACCOUNT_NO and RDN_ACCOUNT_NO in note):
                target_platform = "Bibit"
            elif "gotrade" in note_lower or "valbury" in note_lower:
                target_platform = "Gotrade"
            elif "jenius" in note_lower or "smbc" in note_lower or "btpn" in note_lower:
                target_platform = "Jenius"
            elif "bca" in note_lower and platform != "BCA":
                target_platform = "BCA"
            elif "cash" in note_lower and platform != "Cash":
                target_platform = "Cash"
            else:
                for p_name in ["BCA", "Jenius", "Bibit", "Gotrade", "Cash"]:
                    if p_name.lower() in note_lower and p_name != platform:
                        target_platform = p_name
                        break

        new_tgt_bal = None
        if target_platform:
            cursor.execute("SELECT balance FROM accounts WHERE platform = ?", (target_platform,))
            tgt_row = cursor.fetchone()
            if tgt_row:
                curr_tgt = float(tgt_row[0]) if tgt_row[0] is not None else 0.0
                new_tgt_bal = curr_tgt - amount
                cursor.execute(
                    "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
                    (new_tgt_bal, target_platform),
                )

        fee_text = f" (including Rp {fee:,.2f} fee)" if fee > 0 else ""
        if target_platform and new_tgt_bal is not None:
            summary_msg = (
                f"Reverted balance transfer of Rp {amount:,.2f}{fee_text} from **{platform}** to **{target_platform}**.\n"
                f"• Restored **{platform}** balance: Rp {new_src_bal:,.2f} {currency}\n"
                f"• Restored **{target_platform}** balance: Rp {new_tgt_bal:,.2f} {currency}"
            )
        else:
            summary_msg = (
                f"Reverted outgoing transfer of Rp {amount:,.2f}{fee_text} from **{platform}**.\n"
                f"• Restored **{platform}** balance: Rp {new_src_bal:,.2f} {currency}"
            )

    # 4. Revert Order / order_filled
    elif category == "Order" or tx_type == "order_filled":
        units = None
        ticker = None
        m = re.search(r'([\d\.,]+)\s+([A-Za-z0-9]+)\s+units', note, re.IGNORECASE)
        if m:
            try:
                units = float(m.group(1).replace(",", ""))
                ticker = m.group(2).upper()
            except Exception:
                pass

        if not ticker:
            if platform == "Bibit":
                ticker = "SMMF"
            elif platform == "Gotrade":
                ticker = "VTI"

        reverted_units_msg = ""
        if ticker and units is not None:
            cursor.execute(
                "SELECT units FROM holdings WHERE platform = ? AND ticker = ?",
                (platform, ticker),
            )
            h_row = cursor.fetchone()
            if h_row:
                curr_u = float(h_row[0])
                new_u = max(0.0, curr_u - units)
                cursor.execute(
                    "UPDATE holdings SET units = ? WHERE platform = ? AND ticker = ?",
                    (new_u, platform, ticker),
                )
                reverted_units_msg = f"• Reverted **{platform} ({ticker})** holdings: -{units:,.4f} units (now {new_u:,.4f} units)\n"

        refund_msg = ""
        if amount > 0:
            cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
            acc_row = cursor.fetchone()
            if acc_row:
                curr_bal = float(acc_row[0]) if acc_row[0] is not None else 0.0
                currency = acc_row[1] if acc_row[1] else "IDR"
                new_bal = curr_bal + amount
                cursor.execute(
                    "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
                    (new_bal, platform),
                )
                refund_msg = f"• Refunded cash to **{platform}**: +Rp {amount:,.2f} {currency} (now Rp {new_bal:,.2f} {currency})\n"

        summary_msg = f"Reverted investment order (*{note}*):\n{reverted_units_msg}{refund_msg}".strip()

    else:
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        acc_row = cursor.fetchone()
        if acc_row and amount > 0:
            curr_bal = float(acc_row[0]) if acc_row[0] is not None else 0.0
            currency = acc_row[1] if acc_row[1] else "IDR"
            new_bal = curr_bal + amount
            cursor.execute(
                "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
                (new_bal, platform),
            )
            summary_msg = f"Reverted transaction #{tx_id} (*{note}*): refunded {amount:,.2f} {currency} to **{platform}** (now {new_bal:,.2f} {currency})."
        else:
            summary_msg = f"Reverted transaction #{tx_id} (*{note}*)."

    placeholders = ",".join("?" for _ in ids_to_delete)
    cursor.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids_to_delete)
    conn.commit()
    conn.close()
    return True, summary_msg



# ---------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------
def embed_to_text(embed: discord.Embed) -> str:
    """Converts a discord.Embed into formatted Markdown text for fallback

    when the bot role lacks the 'Embed Links' permission in a channel.
    """
    parts = []
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        parts.append(f"• **{field.name}**: {field.value}")
    return "\n".join(parts)


# ---------------------------------------------------------
# Chart Rendering Helpers (Headless Matplotlib)
# ---------------------------------------------------------
def generate_portfolio_chart(breakdown: dict) -> io.BytesIO:
    """Renders a dark-themed Donut Chart showing portfolio asset allocation.

    Calculates values in IDR for Cash, BCA, Jenius, Bibit (SMMF), and Gotrade (VTI at 16,000 IDR/USD).
    Filters out accounts with 0 balance.
    """
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(6, 6), facecolor="#1e1f22")
    ax.set_facecolor("#1e1f22")

    liquid = breakdown.get("liquid", {})
    investments = breakdown.get("investments", {})

    labels = []
    values = []

    # Liquid accounts
    for acc in ["Cash", "BCA", "Jenius"]:
        bal = float(liquid.get(acc, {}).get("balance", 0.0))
        if bal > 0:
            labels.append(acc)
            values.append(bal)

    # Investments
    bibit_val = float(investments.get("Bibit", 0.0))
    if bibit_val > 0:
        labels.append("Bibit (SMMF)")
        values.append(bibit_val)

    bibit_cash = float(breakdown.get("bibit_cash", 0.0))
    if bibit_cash > 0:
        labels.append("Bibit (Cash)")
        values.append(bibit_cash)

    gotrade_usd = float(investments.get("Gotrade", 0.0))
    if gotrade_usd > 0:
        gotrade_idr = gotrade_usd * 16000.0  # proportional slice visualization
        labels.append("Gotrade (VTI)")
        values.append(gotrade_idr)

    if not values or sum(values) <= 0:
        labels = ["No Assets"]
        values = [1.0]

    colors = ["#4ade80", "#60a5fa", "#a78bfa", "#f472b6", "#fb923c", "#38bdf8"]
    if len(labels) > len(colors):
        colors = colors * ((len(labels) // len(colors)) + 1)
    slice_colors = colors[:len(labels)]

    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels,
        autopct="%1.1f%%" if sum(values) > 0 and labels != ["No Assets"] else "",
        pctdistance=0.75,
        startangle=140,
        colors=slice_colors,
        wedgeprops=dict(width=0.4, edgecolor="#1e1f22", linewidth=2),
        textprops=dict(color="#f3f4f6", fontsize=10, weight="bold"),
    )

    for at in autotexts:
        at.set_color("#ffffff")
        at.set_fontsize(9)

    ax.set_title("Portfolio Asset Allocation", color="#f3f4f6", fontsize=14, weight="bold", pad=20)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=fig.get_facecolor(), transparent=False)
    buf.seek(0)
    plt.close("all")
    return buf


def generate_expenses_chart(days: int = 30, db_name: str = DB_NAME) -> tuple[io.BytesIO | None, dict]:
    """Queries expenses from transactions and renders a horizontal bar chart.

    Groups by category and sums amounts descending.
    Returns (buf, category_totals_dict). If no expenses found, returns (None, {}).
    """
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT category, SUM(amount) as total
        FROM transactions
        WHERE type = 'expense' AND category != 'Fee' AND date >= datetime('now', ?)
        GROUP BY category
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None, {}

    cat_totals = {r[0]: float(r[1]) for r in rows}

    categories = list(cat_totals.keys())
    amounts = list(cat_totals.values())

    categories.reverse()
    amounts.reverse()

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, max(4, len(categories) * 0.6 + 1.5)), facecolor="#1e1f22")
    ax.set_facecolor("#1e1f22")

    bars = ax.barh(categories, amounts, color="#f87171", edgecolor="#1e1f22", height=0.55)

    max_amount = max(amounts) if amounts else 1.0
    ax.set_xlim(0, max_amount * 1.25)

    for bar in bars:
        width = bar.get_width()
        ax.text(
            width + (max_amount * 0.02),
            bar.get_y() + bar.get_height() / 2,
            f"Rp {width:,.0f}",
            va="center",
            ha="left",
            color="#f3f4f6",
            fontsize=9,
            weight="bold",
        )

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#4b5563")
    ax.spines["bottom"].set_color("#4b5563")
    ax.tick_params(colors="#9ca3af", labelsize=9)
    ax.xaxis.grid(True, linestyle="--", alpha=0.3, color="#4b5563")
    ax.set_axisbelow(True)

    ax.set_title(f"Expenses Breakdown (Last {days} Days)", color="#f3f4f6", fontsize=13, weight="bold", pad=15)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=fig.get_facecolor(), transparent=False)
    buf.seek(0)
    plt.close("all")

    return buf, cat_totals


# ---------------------------------------------------------
# Unsloth AI Integration & Robust Slip Parsing
# ---------------------------------------------------------
def clean_and_parse_json(text: str) -> dict:
    """Cleans raw LLM response text, normalizing unicode whitespace (NBSP),

    stripping think tags and code block fences, and extracting valid JSON.
    """
    cleaned = text.replace("\u00a0", " ").replace("\ufeff", "").replace("&nbsp;", " ")
    if "<think>" in cleaned and "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1]
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1:
        cleaned = cleaned[start_idx : end_idx + 1]

    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
    return json.loads(cleaned)


def query_unsloth_chat(prompt: str, system_prompt: str = None) -> str:
    headers = {"Content-Type": "application/json"}
    if UNSLOTH_API_KEY:
        headers["Authorization"] = f"Bearer {UNSLOTH_API_KEY}"

    sys_content = system_prompt or SYSTEM_PERSONA
    payload = {
        "model": UNSLOTH_MODEL,
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": prompt},
        ],
    }
    logger.info(f"Querying Unsloth chat API (model: {UNSLOTH_MODEL})")
    response = requests.post(UNSLOTH_API_URL, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    reply = data["choices"][0]["message"]["content"]
    if "<think>" in reply and "</think>" in reply:
        reply = reply.split("</think>", 1)[1].strip()
    return reply


async def get_financial_critique(topic: str, financial_context: str) -> str:
    master_title = f"Master {USER_NAME}" if USER_NAME else "Master"
    prompt = (
        f"Topic: {topic}\n"
        f"Financial Summary:\n{financial_context}\n\n"
        f"Deliver your financial assessment and remarks to {master_title} (2 to 3 sentences max):"
    )
    try:
        return await asyncio.to_thread(query_unsloth_chat, prompt)
    except Exception as e:
        logger.warning(f"Failed to fetch AI critique from Unsloth: {e}")
        return f"I've compiled the ledger breakdown for your review, {master_title}. Do keep your spending disciplined."


def query_unsloth_vision(image_bytes: bytes) -> dict:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    if image_bytes.startswith(b"\x89PNG"):
        mime_type = "image/png"
    elif image_bytes.startswith(b"\xff\xd8"):
        mime_type = "image/jpeg"
    elif image_bytes.startswith(b"GIF8"):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"

    image_data_url = f"data:{mime_type};base64,{base64_image}"

    prompt = f"""Extract transaction details from this receipt or financial slip image.
User Identity & Tracked Accounts:
- User Name: {USER_NAME}
- BCA Account Number: {BCA_ACCOUNT_NO}
- Jenius / Bank BTPN / Bank SMBC Indonesia Account Number: {JENIUS_ACCOUNT_NO}
- Bibit RDN Account Number: {RDN_ACCOUNT_NO}

Platform Detection Rules:
- If the slip is from BCA m-banking (m-Transfer with BCA watermark): source platform = "BCA".
- If the slip is from Jenius app (titled "Outgoing Transfer" or Jenius UI style): source platform = "Jenius", and the bank listed under recipient name (e.g. "BCA • {BCA_ACCOUNT_NO}") is target_platform = "BCA".

Classification Rules:
1. "switching": Balance switching / transfer between user's own accounts or investment top-ups:
   - BCA to Jenius (Bank SMBC Indonesia / Bank BTPN)
   - Jenius to BCA
   - If the recipient account matches RDN_ACCOUNT_NO, or recipient mentions Bibit, set kind = "switching" and target_platform = "Bibit"
   - If recipient mentions PT Valbury Asia Futures or Gotrade, set kind = "switching" and target_platform = "Gotrade"
   - target_platform must be one of: "BCA" | "Jenius" | "Bibit" | "Gotrade"
2. "transfer": Outgoing transfer to an external person or third party (recipient is NOT {USER_NAME} and NOT user's RDN/investment accounts).
   - target_platform must be: "External"
3. "expense": Direct purchases, groceries, fuel, bills, QRIS.
4. "income": Incoming money/deposit.
5. "order_filled": Stock or mutual fund order execution (VTI, SMMF).

Note Formatting:
- For any transfer or switching, format the note strictly as: "transfer to <bank name> <recipient name>" (e.g. "transfer to BCA {USER_NAME}" or "transfer to BANK BRI RECIPIENT NAME" or "transfer to BIBIT RDN {USER_NAME}").

Fee Rule:
- Put any admin/transfer fee in "fee" as a float (e.g. 2500.0). If no fee, null or 0.

Return STRICT JSON ONLY with schema:
{{
  "kind": "switching" | "transfer" | "expense" | "income" | "order_filled",
  "platform": "BCA" | "Jenius" | "Bibit" | "Gotrade" | "Cash",
  "target_platform": "BCA" | "Jenius" | "Bibit" | "Gotrade" | "External" | null,
  "amount": float,
  "fee": float or null,
  "category": string,
  "note": string,
  "ticker": string or null,
  "units_added": float or null
}}"""

    headers = {"Content-Type": "application/json"}
    if UNSLOTH_API_KEY:
        headers["Authorization"] = f"Bearer {UNSLOTH_API_KEY}"

    payload = {
        "model": UNSLOTH_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}}
                ]
            }
        ],
        "temperature": 0.1
    }

    logger.info(f"Querying Unsloth vision API for slip extraction ({len(image_bytes)} bytes, mime={mime_type})")
    response = requests.post(UNSLOTH_API_URL, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    raw_content = data["choices"][0]["message"]["content"].strip()
    return clean_and_parse_json(raw_content)


def process_parsed_slip(data: dict, db_name: str = DB_NAME) -> discord.Embed:
    kind = str(data.get("kind") or "").strip().lower()
    platform_raw = str(data.get("platform") or "").strip()
    platform = PLATFORM_MAP.get(platform_raw.lower(), platform_raw)

    target_raw = str(data.get("target_platform") or "").strip()
    target_platform = PLATFORM_MAP.get(target_raw.lower(), target_raw if target_raw else "External")

    amount = float(data.get("amount") or 0.0)
    fee = float(data.get("fee") or 0.0)
    category = str(data.get("category") or "General").strip()
    note = str(data.get("note") or "").strip()[:1000]
    ticker = data.get("ticker")
    units_added = float(data.get("units_added") or 0.0)

    logger.info(f"Processing slip on '{db_name}': kind='{kind}', platform='{platform}', amount={amount:,.2f}, fee={fee:,.2f}")

    # Fallback fee detection from note string if fee was 0
    if fee == 0.0 and note:
        fee_match = re.search(r"(?:BIAYA|FEE|ADMIN)[\s:Rp\.]*([\d\.,]+)", note, re.IGNORECASE)
        if fee_match:
            try:
                fee_clean = fee_match.group(1).replace(".", "").replace(",", ".")
                fee = float(fee_clean)
            except Exception:
                pass

    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    if kind == "expense":
        total_deducted = amount + fee
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        row = cursor.fetchone()
        curr_balance = float(row[0]) if row and row[0] is not None else 0.0
        currency = row[1] if row and row[1] else "IDR"
        new_balance = curr_balance - total_deducted
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_balance, platform),
        )
        cursor.execute(
            """
            INSERT INTO transactions (platform, type, amount, category, note)
            VALUES (?, 'expense', ?, ?, ?)
            """,
            (platform, amount, category, note),
        )
        if fee > 0:
            cursor.execute(
                """
                INSERT INTO transactions (platform, type, amount, category, note)
                VALUES (?, 'expense', ?, 'Fee', ?)
                """,
                (platform, fee, f"Admin fee: {note}"),
            )
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="💸 Expense Recorded",
            color=discord.Color.red()
        )
        embed.add_field(name="Platform", value=platform, inline=True)
        embed.add_field(name="Amount", value=f"{amount:,.2f} {currency}", inline=True)
        if fee > 0:
            embed.add_field(name="Admin Fee", value=f"{fee:,.2f} {currency}", inline=True)
            embed.add_field(name="Total Deducted", value=f"{total_deducted:,.2f} {currency}", inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Note", value=note if note else "-", inline=False)
        embed.add_field(name="Remaining Balance", value=f"{new_balance:,.2f} {currency}", inline=False)
        return embed

    elif kind == "income":
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        row = cursor.fetchone()
        curr_balance = float(row[0]) if row and row[0] is not None else 0.0
        currency = row[1] if row and row[1] else "IDR"
        new_balance = curr_balance + amount
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_balance, platform),
        )
        cursor.execute(
            """
            INSERT INTO transactions (platform, type, amount, category, note)
            VALUES (?, 'income', ?, ?, ?)
            """,
            (platform, amount, category, note),
        )
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="💵 Income Recorded",
            color=discord.Color.green()
        )
        embed.add_field(name="Platform", value=platform, inline=True)
        embed.add_field(name="Amount", value=f"{amount:,.2f} {currency}", inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Note", value=note if note else "-", inline=False)
        embed.add_field(name="Updated Balance", value=f"{new_balance:,.2f} {currency}", inline=False)
        return embed

    elif kind == "switching":
        # Internal balance switching / RDN funding
        total_deducted = amount + fee

        # Deduct from source account
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        row_src = cursor.fetchone()
        curr_src_bal = float(row_src[0]) if row_src and row_src[0] is not None else 0.0
        currency = row_src[1] if row_src and row_src[1] else "IDR"
        new_src_bal = curr_src_bal - total_deducted
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_src_bal, platform),
        )

        # Credit to target account if in accounts (BCA, Jenius, Bibit, Gotrade)
        new_target_bal = None
        if target_platform and target_platform in PLATFORM_MAP.values():
            cursor.execute("SELECT balance FROM accounts WHERE platform = ?", (target_platform,))
            row_tgt = cursor.fetchone()
            curr_tgt_bal = float(row_tgt[0]) if row_tgt and row_tgt[0] is not None else 0.0
            new_target_bal = curr_tgt_bal + amount
            cursor.execute(
                "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
                (new_target_bal, target_platform),
            )

        # Log transaction: category is "Investment Funding" for Bibit/Gotrade, else "Switching"
        category_logged = "Investment Funding" if target_platform in ["Bibit", "Gotrade"] else "Switching"
        cursor.execute(
            """
            INSERT INTO transactions (platform, type, amount, category, note)
            VALUES (?, 'transfer', ?, ?, ?)
            """,
            (platform, amount, category_logged, note),
        )
        if fee > 0:
            cursor.execute(
                """
                INSERT INTO transactions (platform, type, amount, category, note)
                VALUES (?, 'expense', ?, 'Fee', ?)
                """,
                (platform, fee, f"Transfer fee for: {note}"),
            )
        conn.commit()
        conn.close()

        is_investment = category_logged == "Investment Funding"
        embed = discord.Embed(
            title="📈 Investment Funding" if is_investment else "↔️ Balance Switching",
            color=discord.Color.teal() if is_investment else discord.Color.blue()
        )
        embed.add_field(name="From", value=platform, inline=True)
        embed.add_field(name="To", value=target_platform if target_platform else "Internal", inline=True)
        embed.add_field(name="Funded Amount" if is_investment else "Switched Amount", value=f"{amount:,.2f} {currency}", inline=True)
        if fee > 0:
            embed.add_field(name="Transfer Fee", value=f"{fee:,.2f} {currency}", inline=True)
            embed.add_field(name="Total Deducted", value=f"{total_deducted:,.2f} {currency}", inline=True)
        embed.add_field(name="Note", value=note if note else "-", inline=False)
        bal_text = f"• **{platform}**: Rp {new_src_bal:,.2f} {currency}"
        if new_target_bal is not None:
            bal_text += f"\n• **{target_platform}**: Rp {new_target_bal:,.2f} {currency}"
        embed.add_field(name="Updated Balances", value=bal_text, inline=False)
        return embed

    elif kind == "transfer":
        # Check if this transfer is actually a self-transfer (me to me) or RDN funding
        is_rdn_bibit = (RDN_ACCOUNT_NO and RDN_ACCOUNT_NO in note) or "bibit" in note.lower()
        is_rdn_gotrade = any(kw in note.lower() for kw in ["valbury", "gotrade"])
        is_self = (
            target_platform in ["BCA", "Jenius", "Bibit", "Gotrade"]
            or (USER_NAME and USER_NAME.lower() in note.lower())
            or is_rdn_bibit
            or is_rdn_gotrade
        )
        if is_self:
            conn.close()
            data["kind"] = "switching"
            if target_platform not in ["BCA", "Jenius", "Bibit", "Gotrade"]:
                if is_rdn_gotrade:
                    data["target_platform"] = "Gotrade"
                elif is_rdn_bibit:
                    data["target_platform"] = "Bibit"
            return process_parsed_slip(data, db_name=db_name)

        # Outgoing transfer to external third party
        total_deducted = amount + fee
        cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
        row = cursor.fetchone()
        curr_balance = float(row[0]) if row and row[0] is not None else 0.0
        currency = row[1] if row and row[1] else "IDR"
        new_balance = curr_balance - total_deducted
        cursor.execute(
            "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (new_balance, platform),
        )
        cursor.execute(
            """
            INSERT INTO transactions (platform, type, amount, category, note)
            VALUES (?, 'transfer', ?, 'Outgoing', ?)
            """,
            (platform, amount, note),
        )
        if fee > 0:
            cursor.execute(
                """
                INSERT INTO transactions (platform, type, amount, category, note)
                VALUES (?, 'expense', ?, 'Fee', ?)
                """,
                (platform, fee, f"Transfer fee for: {note}"),
            )
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="➡️ Outgoing Transfer",
            color=discord.Color.orange()
        )
        embed.add_field(name="From", value=platform, inline=True)
        embed.add_field(name="To", value=target_platform if target_platform else "External", inline=True)
        embed.add_field(name="Transfer Amount", value=f"{amount:,.2f} {currency}", inline=True)
        if fee > 0:
            embed.add_field(name="Transfer Fee", value=f"{fee:,.2f} {currency}", inline=True)
            embed.add_field(name="Total Deducted", value=f"{total_deducted:,.2f} {currency}", inline=True)
        embed.add_field(name="Note", value=note if note else "-", inline=False)
        embed.add_field(name="Remaining Balance", value=f"{new_balance:,.2f} {currency}", inline=False)
        return embed

    elif kind == "order_filled":
        cursor.execute(
            "SELECT units, currency FROM holdings WHERE platform = ? AND ticker = ?",
            (platform, ticker),
        )
        row = cursor.fetchone()
        if row:
            curr_units = float(row[0])
            currency = row[1]
            new_units = curr_units + units_added
            cursor.execute(
                "UPDATE holdings SET units = ? WHERE platform = ? AND ticker = ?",
                (new_units, platform, ticker),
            )
        else:
            currency = "USD" if platform == "Gotrade" else "IDR"
            new_units = units_added
            cursor.execute(
                "INSERT INTO holdings (platform, ticker, units, currency) VALUES (?, ?, ?, ?)",
                (platform, ticker, new_units, currency),
            )

        new_account_bal = None
        if amount and amount > 0:
            cursor.execute("SELECT balance, currency FROM accounts WHERE platform = ?", (platform,))
            row_acc = cursor.fetchone()
            if row_acc:
                curr_acc = float(row_acc[0]) if row_acc[0] is not None else 0.0
                currency = row_acc[1] if row_acc[1] else currency
                new_account_bal = curr_acc - amount
                cursor.execute(
                    "UPDATE accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
                    (new_account_bal, platform),
                )

        cursor.execute(
            """
            INSERT INTO transactions (platform, type, amount, category, note)
            VALUES (?, 'order_filled', ?, 'Order', ?)
            """,
            (platform, amount if amount else 0.0, note if note else f"Purchase of {units_added} {ticker} units"),
        )

        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="📈 Order Filled / Units Added",
            color=discord.Color.purple()
        )
        embed.add_field(name="Platform", value=platform, inline=True)
        embed.add_field(name="Ticker", value=ticker if ticker else "-", inline=True)
        embed.add_field(name="Units Added", value=f"{units_added:,.4f}", inline=True)
        embed.add_field(name="Total Units", value=f"{new_units:,.4f}", inline=False)
        if amount and amount > 0:
            embed.add_field(name="Order Amount", value=f"{amount:,.2f} {currency}", inline=True)
            if new_account_bal is not None:
                embed.add_field(name="Remaining Cash Balance", value=f"{new_account_bal:,.2f} {currency}", inline=True)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        return embed

    conn.close()
    raise ValueError(f"Unknown slip kind: {kind}")


def commit_parsed_slip(data: dict, db_name: str = DB_NAME) -> discord.Embed:
    """Commits a parsed slip to the database and runs the biweekly backup check."""
    embed = process_parsed_slip(data, db_name=db_name)
    try:
        check_and_run_biweekly_backup(db_name=db_name)
    except Exception as e:
        logger.warning(f"Failed biweekly backup check after slip processing: {e}")
    return embed


# ---------------------------------------------------------
# Discord Bot Setup & Event Listeners
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}", exc_info=True)
    try:
        backup_path = check_and_run_biweekly_backup()
        if backup_path:
            logger.info(f"Biweekly automatic backup created at: {backup_path}")
    except Exception as e:
        logger.error(f"Failed biweekly backup check: {e}", exc_info=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # If message.attachments is EMPTY: purely conversational text chat (non-blocking in thread pool)
    if not message.attachments:
        if message.content.strip():
            async with message.channel.typing():
                try:
                    reply_text = await asyncio.to_thread(query_unsloth_chat, message.content)
                    await message.reply(reply_text)
                except Exception as e:
                    logger.error(f"Chat error: {e}", exc_info=True)
                    try:
                        await message.reply("Sorry, I encountered an error talking to the AI service.")
                    except Exception:
                        pass
        return

    # If message.attachments has an image: receipt/financial slip extraction
    first_attachment = message.attachments[0]
    content_type = first_attachment.content_type or ""
    is_image = content_type.startswith("image/") or any(
        first_attachment.filename.lower().endswith(ext)
        for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]
    )

    if is_image:
        async with message.channel.typing():
            try:
                image_bytes = await first_attachment.read()
                data = await asyncio.to_thread(query_unsloth_vision, image_bytes)
                embed = await asyncio.to_thread(commit_parsed_slip, data)
            except Exception as e:
                logger.error(f"Error extracting or processing receipt slip: {e}", exc_info=True)
                try:
                    await message.reply(
                        "❌ Unable to parse this receipt or financial slip. "
                        "Please ensure the image is clear and contains transaction details."
                    )
                except Exception:
                    pass
                return

            # Reply with Embed, gracefully falling back to text if 'Embed Links' permission is missing
            try:
                await message.reply(embed=embed)
            except discord.Forbidden:
                fallback_text = embed_to_text(embed)
                try:
                    await message.reply(
                        f"{fallback_text}\n\n"
                        "> ⚠️ **Permission Notice**: The bot role is missing the **Embed Links** permission in this channel. "
                        "Please grant **Embed Links** to the bot role in your Discord Server Settings to see rich styled cards!"
                    )
                except discord.Forbidden:
                    logger.error(f"Bot lacks permission to send messages in channel {message.channel.id}")
            except Exception as e:
                logger.error(f"Error sending embed response: {e}", exc_info=True)
    else:
        if message.content.strip():
            async with message.channel.typing():
                try:
                    reply_text = await asyncio.to_thread(query_unsloth_chat, message.content)
                    await message.reply(reply_text)
                except Exception as e:
                    logger.error(f"Chat error: {e}", exc_info=True)
                    try:
                        await message.reply("Sorry, I encountered an error talking to the AI service.")
                    except Exception:
                        pass


# ---------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------
@bot.tree.command(name="checkbalance", description="View portfolio and balance breakdown across all accounts")
async def checkbalance(interaction: discord.Interaction):
    logger.info(f"Command /checkbalance executed by {interaction.user}")
    await interaction.response.defer()
    breakdown = await asyncio.to_thread(get_balance_breakdown)

    embed = discord.Embed(
        title="🏦 Portfolio & Balance Summary",
        color=discord.Color.blue()
    )

    liquid = breakdown["liquid"]
    cash_val = liquid.get("Cash", {}).get("balance", 0.0)
    bca_val = liquid.get("BCA", {}).get("balance", 0.0)
    jenius_val = liquid.get("Jenius", {}).get("balance", 0.0)
    total_liquid = breakdown.get("total_liquid_idr", cash_val + bca_val + jenius_val)

    liquid_text = (
        f"• **Cash**: Rp {cash_val:,.2f}\n"
        f"• **BCA**: Rp {bca_val:,.2f}\n"
        f"• **Jenius**: Rp {jenius_val:,.2f}\n"
        f"**Subtotal**: Rp {total_liquid:,.2f}"
    )
    embed.add_field(name="💵 Liquid Cash & Banks", value=liquid_text, inline=False)

    investments = breakdown["investments"]
    bibit_val = investments.get("Bibit", 0.0)
    bibit_cash = breakdown.get("bibit_cash", 0.0)
    gotrade_val = investments.get("Gotrade", 0.0)

    investments_lines = [
        f"• **Bibit (SMMF)**: Rp {bibit_val:,.2f} IDR",
    ]
    if bibit_cash > 0:
        investments_lines.append(f"• **Bibit (RDN Cash)**: Rp {bibit_cash:,.2f} IDR")
    investments_lines.append(f"• **Gotrade (VTI)**: ${gotrade_val:,.2f} USD")
    investments_text = "\n".join(investments_lines)
    embed.add_field(name="📈 Investments", value=investments_text, inline=False)

    total_idr = breakdown.get("total_idr", total_liquid + bibit_val + bibit_cash)
    total_usd = breakdown.get("total_usd", gotrade_val)
    net_worth_text = (
        f"• **Total IDR Assets**: Rp {total_idr:,.2f}\n"
        f"• **Total USD Assets**: ${total_usd:,.2f}"
    )
    embed.add_field(name="💎 Total Net Worth", value=net_worth_text, inline=False)

    try:
        await interaction.followup.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send(embed_to_text(embed))


@bot.tree.command(name="cashin", description="Record cash inflow/income to your physical wallet")
@app_commands.describe(amount="Amount of cash added in IDR", note="Description or source of cash")
async def cashin(interaction: discord.Interaction, amount: float, note: str):
    logger.info(f"Command /cashin executed by {interaction.user}: amount={amount}, note='{note}'")
    new_balance = record_cash_flow(amount, "income", note)
    embed = discord.Embed(
        title="💵 Cash Inflow Added",
        color=discord.Color.green()
    )
    embed.add_field(name="Amount Added", value=f"Rp {amount:,.2f}", inline=True)
    embed.add_field(name="Note", value=note, inline=True)
    embed.add_field(name="Updated Wallet Total", value=f"Rp {new_balance:,.2f}", inline=False)
    try:
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed_to_text(embed))


@bot.tree.command(name="cashout", description="Record cash outflow/expense from your physical wallet")
@app_commands.describe(amount="Amount of cash spent in IDR", note="Description or purpose of expense")
async def cashout(interaction: discord.Interaction, amount: float, note: str):
    logger.info(f"Command /cashout executed by {interaction.user}: amount={amount}, note='{note}'")
    new_balance = record_cash_flow(amount, "expense", note)
    embed = discord.Embed(
        title="💸 Cash Outflow Logged",
        color=discord.Color.red()
    )
    embed.add_field(name="Amount Spent", value=f"Rp {amount:,.2f}", inline=True)
    embed.add_field(name="Note", value=note, inline=True)
    embed.add_field(name="Remaining Wallet Balance", value=f"Rp {new_balance:,.2f}", inline=False)
    try:
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed_to_text(embed))


@bot.tree.command(name="checkactivity", description="List recent activity (expenses, outgoing transfers, balance switching) from the last N days")
@app_commands.describe(days="Number of days to check (default: 7)")
async def checkactivity(interaction: discord.Interaction, days: int = 7):
    logger.info(f"Command /checkactivity executed by {interaction.user}: days={days}")
    rows, total_outflow, total_switched = get_recent_activity(days)
    embed = discord.Embed(
        title=f"📊 Recent Activity (Last {days} Days)",
        color=discord.Color.gold()
    )
    if rows:
        lines = []
        for r in rows[:15]:
            date_str, platform, tx_type, category, amount, note = r[0], r[1], r[2], r[3], float(r[4]), r[5]
            if tx_type == "transfer" and category in ("Switching", "Investment Funding"):
                icon = "↔️"
            elif tx_type == "transfer":
                icon = "➡️"
            elif category == "Fee":
                icon = "🏷️"
            elif tx_type == "expense":
                icon = "💸"
            elif tx_type == "income":
                icon = "💵"
            else:
                icon = "📝"
            lines.append(f"• `{date_str}` | **{platform}** | Rp {amount:,.2f} - {icon} *{note}*")
        embed.description = "\n".join(lines)
    else:
        embed.description = f"No activity recorded in the last {days} days."

    embed.add_field(name=f"Total Outflow ({days} Days)", value=f"Rp {total_outflow:,.2f}\n*(Expenses + Third-Party Transfers)*", inline=True)
    embed.add_field(name=f"Total Switched ({days} Days)", value=f"Rp {total_switched:,.2f}\n*(Me-to-Me & RDN Deposits)*", inline=True)
    try:
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed_to_text(embed))


@bot.tree.command(name="checkincome", description="List recent income/inflows from the last 7 days")
async def checkincome(interaction: discord.Interaction):
    logger.info(f"Command /checkincome executed by {interaction.user}")
    rows, total_amount = get_recent_summary("income", 7)
    embed = discord.Embed(
        title="📈 Inflow / Income (Last 7 Days)",
        color=discord.Color.teal()
    )
    if rows:
        lines = []
        for r in rows[:15]:
            date_str, platform, amount, note = r[0], r[1], float(r[2]), r[3]
            lines.append(f"• `{date_str}` | **{platform}** | Rp {amount:,.2f} - *{note}*")
        embed.description = "\n".join(lines)
    else:
        embed.description = "No income recorded in the last 7 days."

    embed.add_field(name="Total (7 Days)", value=f"Rp {total_amount:,.2f}", inline=False)
    try:
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed_to_text(embed))


@bot.tree.command(name="undo", description="Revert the most recently recorded transaction")
async def undo(interaction: discord.Interaction):
    logger.info(f"Command /undo executed by {interaction.user}")
    success, message = undo_last_transaction()
    if success:
        embed = discord.Embed(
            title="🔄 Transaction Reverted",
            description=message,
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="⚠️ Undo Failed",
            description=message,
            color=discord.Color.red(),
        )
    try:
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed_to_text(embed))


# ---------------------------------------------------------
# Slash Command Group (/chart)
# ---------------------------------------------------------
chart_group = app_commands.Group(name="chart", description="Visual charts and Kaf's financial remarks")


@chart_group.command(name="portfolio", description="View dark-themed donut chart of asset allocation with Kaf's commentary")
async def chart_portfolio(interaction: discord.Interaction):
    logger.info(f"Command /chart portfolio executed by {interaction.user}")
    await interaction.response.defer()
    breakdown = await asyncio.to_thread(get_balance_breakdown)
    buf = await asyncio.to_thread(generate_portfolio_chart, breakdown)

    liquid = breakdown.get("liquid", {})
    investments = breakdown.get("investments", {})
    cash_val = liquid.get("Cash", {}).get("balance", 0.0)
    bca_val = liquid.get("BCA", {}).get("balance", 0.0)
    jenius_val = liquid.get("Jenius", {}).get("balance", 0.0)
    bibit_val = investments.get("Bibit", 0.0)
    bibit_cash = breakdown.get("bibit_cash", 0.0)
    gotrade_val = investments.get("Gotrade", 0.0)

    summary_lines = [
        f"• Cash: Rp {cash_val:,.2f}",
        f"• BCA: Rp {bca_val:,.2f}",
        f"• Jenius: Rp {jenius_val:,.2f}",
        f"• Bibit (SMMF): Rp {bibit_val:,.2f} IDR",
    ]
    if bibit_cash > 0:
        summary_lines.append(f"• Bibit (RDN Cash): Rp {bibit_cash:,.2f} IDR")
    summary_lines.extend([
        f"• Gotrade (VTI): ${gotrade_val:,.2f} USD",
        f"• Total IDR Assets: Rp {breakdown.get('total_idr', 0.0):,.2f}",
        f"• Total USD Assets: ${breakdown.get('total_usd', 0.0):,.2f}",
    ])
    summary_text = "\n".join(summary_lines)
    commentary = await get_financial_critique("Portfolio Asset Allocation", summary_text)

    embed = discord.Embed(
        title="📊 Portfolio Allocation",
        description=commentary,
        color=discord.Color(0x2B2D31),
    )
    chart_file = discord.File(buf, filename="chart.png")
    embed.set_image(url="attachment://chart.png")
    try:
        await interaction.followup.send(embed=embed, file=chart_file)
    except discord.Forbidden:
        chart_file.seek(0)
        await interaction.followup.send(
            content=f"📊 **Portfolio Allocation**\n\n{commentary}",
            file=chart_file,
        )


@chart_group.command(name="expenses", description="View horizontal bar chart of recent expenses with Kaf's commentary")
@app_commands.describe(days="Number of days to analyze (default: 30)")
async def chart_expenses(interaction: discord.Interaction, days: int = 30):
    logger.info(f"Command /chart expenses executed by {interaction.user}: days={days}")
    await interaction.response.defer()
    result = await asyncio.to_thread(generate_expenses_chart, days=days)
    buf, cat_totals = result
    if buf is None:
        await interaction.followup.send(f"No expenses recorded in the last {days} days.")
        return

    total_spent = sum(cat_totals.values())
    cat_summary = "\n".join([f"• {cat}: Rp {amt:,.2f}" for cat, amt in cat_totals.items()])
    summary_text = f"Total Outflow: Rp {total_spent:,.2f}\nCategories:\n{cat_summary}"
    commentary = await get_financial_critique("Recent Spending Breakdown", summary_text)

    embed = discord.Embed(
        title=f"💸 Spending Breakdown (Last {days} Days)",
        description=commentary,
        color=discord.Color(0x2B2D31),
    )
    chart_file = discord.File(buf, filename="chart.png")
    embed.set_image(url="attachment://chart.png")
    try:
        await interaction.followup.send(embed=embed, file=chart_file)
    except discord.Forbidden:
        chart_file.seek(0)
        await interaction.followup.send(
            content=f"💸 **Spending Breakdown (Last {days} Days)**\n\n{commentary}",
            file=chart_file,
        )


bot.tree.add_command(chart_group)


@bot.tree.command(name="backup", description="Create an atomic SQLite backup and upload the .db file")
async def backup(interaction: discord.Interaction):
    logger.info(f"Command /backup executed by {interaction.user}")
    await interaction.response.defer()
    try:
        backup_path = await asyncio.to_thread(perform_db_backup, DB_NAME)
        size_kb = os.path.getsize(backup_path) / 1024.0
        filename = os.path.basename(backup_path)

        embed = discord.Embed(
            title="💾 Database Backup Created",
            color=discord.Color.green(),
        )
        embed.add_field(name="Filename", value=f"`{filename}`", inline=True)
        embed.add_field(name="File Size", value=f"{size_kb:.2f} KB", inline=True)
        embed.set_footer(text="WAL-safe atomic SQLite snapshot")

        db_file = discord.File(backup_path, filename=filename)
        try:
            await interaction.followup.send(embed=embed, file=db_file)
        except discord.Forbidden:
            db_file.seek(0)
            await interaction.followup.send(
                content=f"💾 **Database Backup Created**\n• File: `{filename}`\n• Size: {size_kb:.2f} KB",
                file=db_file,
            )
    except Exception as e:
        logger.error(f"Backup command error: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Failed to create database backup: {e}")


# ---------------------------------------------------------
# Main Entry Point & Smoke Test
# ---------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--test" in sys.argv:
        # Keep --test CLI output clean and readable on stdout while bot.log records full INFO logs
        stream_handler.setLevel(logging.WARNING)
        print("=== RUNNING CLI SMOKE TEST (--test) ===")
        TEST_DB = "test_finance.db"

        # Clean up any pre-existing test database files
        for f in [TEST_DB, f"{TEST_DB}-wal", f"{TEST_DB}-shm"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        # 1. Test isolated database initialization & WAL mode verification
        init_db(TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        wal_mode = cursor.fetchone()[0]
        conn.close()
        print(f"Verified SQLite journal_mode: {wal_mode}")
        assert wal_mode.lower() == "wal", f"Expected 'wal', got {wal_mode}"

        # 2. Dynamic Friday-Aligned SMMF NAV Fetcher Verification
        print("\n[PART 0: Dynamic SMMF NAV Fetcher Verification]")
        live_nav = fetch_smmf_nav(force_refresh=True)
        print(f"Retrieved live SMMF NAV: {live_nav}")
        assert 1500.0 < live_nav < 3000.0, f"NAV out of bounds (1500.0 < nav < 3000.0): {live_nav}"
        print("Live SMMF NAV verified within realistic bounds.")

        # Verify cache file was written with live NAV
        with open(NAV_CACHE_FILE, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
            assert cache_data.get("nav") == live_nav, "Cache does not match live NAV!"

        # Verify network failure fallback uses dynamic cached NAV, NOT static seed
        import unittest.mock
        with unittest.mock.patch("requests.get", side_effect=Exception("Network simulated failure")):
            fallback_nav = fetch_smmf_nav(force_refresh=True)
            print(f"Verified network failure fallback with cache: {fallback_nav} (expected cached {live_nav})")
            assert fallback_nav == live_nav, f"Expected {live_nav}, got {fallback_nav}"
        print("Dynamic NAV cache fallback verified cleanly.")

        # 3. Part A: Cash Flow Verification on TEST_DB
        print("\n[PART A: Feature 3 Cash Flow (test_finance.db)]")
        cash_after_income = record_cash_flow(100000, "income", "ATM withdrawal", db_name=TEST_DB)
        print(f"1. Cash balance after income: {cash_after_income}")
        assert cash_after_income == 100000.0, f"Expected 100000.0, got {cash_after_income}"

        cash_after_expense = record_cash_flow(25000, "expense", "Lunch", db_name=TEST_DB)
        print(f"2. Cash balance after expense: {cash_after_expense}")
        assert cash_after_expense == 75000.0, f"Expected 75000.0, got {cash_after_expense}"

        breakdown = get_balance_breakdown(db_name=TEST_DB)
        print(f"3. Breakdown: {breakdown}")
        assert breakdown["liquid"]["Cash"]["balance"] == 75000.0

        outflow_rows, outflow_total = get_recent_outflows(7, db_name=TEST_DB)
        print(f"4. Total Outflows: {outflow_total}, Count: {len(outflow_rows)}")
        assert outflow_total == 25000.0
        print("Part A checks PASSED.")

        # 4. Part B: Feature 5 RDN Routing & Order Execution on TEST_DB
        print("\n[PART B: Feature 5 Verification (test_finance.db)]")
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Bibit'")
        bibit_cash_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT units FROM holdings WHERE platform = 'Bibit' AND ticker = 'SMMF'")
        smmf_units_start = float(cursor.fetchone()[0])
        conn.close()

        # Test 1: RDN Transfer: BCA -> Bibit Rp 1.000.000
        mock_rdn = {
            "kind": "switching",
            "platform": "BCA",
            "target_platform": "Bibit",
            "amount": 1000000.0,
            "fee": None,
            "category": "Investment Funding",
            "note": f"transfer to BIBIT RDN {USER_NAME}",
            "ticker": None,
            "units_added": None
        }
        embed_rdn = process_parsed_slip(mock_rdn, db_name=TEST_DB)
        assert isinstance(embed_rdn, discord.Embed)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_after_rdn = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Bibit'")
        bibit_cash_after_rdn = float(cursor.fetchone()[0])
        conn.close()

        bca_diff = bca_after_rdn - bca_start
        bibit_diff = bibit_cash_after_rdn - bibit_cash_start
        print(f"1. RDN Transfer: BCA diff={bca_diff} (expected -1000000.0), Bibit cash diff={bibit_diff} (expected +1000000.0)")
        assert round(bca_diff, 2) == -1000000.0, f"BCA balance decrease mismatch: {bca_diff}"
        assert round(bibit_diff, 2) == 1000000.0, f"Bibit cash balance increase mismatch: {bibit_diff}"

        # Verify uninvested RDN cash is accounted for in breakdown
        rdn_breakdown = get_balance_breakdown(db_name=TEST_DB)
        assert rdn_breakdown["bibit_cash"] == 1000000.0, f"Expected bibit_cash 1000000.0, got {rdn_breakdown['bibit_cash']}"
        print(f"1b. Verified uninvested Bibit RDN cash in breakdown: Rp {rdn_breakdown['bibit_cash']:,.2f}")

        # Test 2: Order Filled: Bibit purchase of 251.05 SMMF units for Rp 500.000
        mock_order = {
            "kind": "order_filled",
            "platform": "Bibit",
            "target_platform": None,
            "amount": 500000.0,
            "fee": None,
            "category": "Investment",
            "note": "Bibit purchase of 251.05 SMMF units",
            "ticker": "SMMF",
            "units_added": 251.05
        }
        embed_order = process_parsed_slip(mock_order, db_name=TEST_DB)
        assert isinstance(embed_order, discord.Embed)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT units FROM holdings WHERE platform = 'Bibit' AND ticker = 'SMMF'")
        smmf_units_after = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Bibit'")
        bibit_cash_after_order = float(cursor.fetchone()[0])
        conn.close()

        smmf_diff = smmf_units_after - smmf_units_start
        bibit_order_diff = bibit_cash_after_order - bibit_cash_after_rdn
        print(f"2. Order Filled: SMMF units diff={smmf_diff} (expected +251.05), Bibit cash diff={bibit_order_diff} (expected -500000.0)")
        assert round(smmf_diff, 4) == 251.05, f"SMMF units increase mismatch: {smmf_diff}"
        assert round(bibit_order_diff, 2) == -500000.0, f"Bibit cash decrease mismatch: {bibit_order_diff}"

        post_order_breakdown = get_balance_breakdown(db_name=TEST_DB)
        assert post_order_breakdown["bibit_cash"] == 500000.0, f"Expected bibit_cash 500000.0, got {post_order_breakdown['bibit_cash']}"
        print(f"2b. Verified remaining Bibit RDN cash in breakdown: Rp {post_order_breakdown['bibit_cash']:,.2f}")

        # Test 3: Command verification: Call get_recent_activity(days=14, db_name=TEST_DB)
        act_rows, act_outflows, act_switched = get_recent_activity(days=14, db_name=TEST_DB)
        print(f"3. get_recent_activity(days=14): {len(act_rows)} items, Outflow=Rp {act_outflows:,.2f}, Switched=Rp {act_switched:,.2f}")
        assert isinstance(act_rows, list)
        assert isinstance(act_outflows, float)
        assert isinstance(act_switched, float)
        notes = [r[5] for r in act_rows]
        assert f"transfer to BIBIT RDN {USER_NAME}" in notes, "Missing RDN transfer in recent activity!"

        # 5. Part C: Feature 6 Undo Verification on TEST_DB
        print("\n[PART C: Feature 6 Undo Verification (test_finance.db)]")
        # Clear existing test transactions to assert table becomes completely empty
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transactions;")
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_pre_undo = float(cursor.fetchone()[0])
        conn.commit()
        conn.close()

        # 1. Record an expense (Rp 50.000)
        mock_expense = {
            "kind": "expense",
            "platform": "BCA",
            "target_platform": None,
            "amount": 50000.0,
            "fee": None,
            "category": "Food",
            "note": "Dinner",
            "ticker": None,
            "units_added": None
        }
        process_parsed_slip(mock_expense, db_name=TEST_DB)

        # 2. Verify balance decreased
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_after_exp = float(cursor.fetchone()[0])
        conn.close()
        print(f"1 & 2. Recorded expense Rp 50,000, BCA balance decreased from {bca_pre_undo} to {bca_after_exp}")
        assert bca_after_exp == bca_pre_undo - 50000.0, f"Expected {bca_pre_undo - 50000.0}, got {bca_after_exp}"

        # 3. Call undo_last_transaction(TEST_DB)
        success, undo_msg = undo_last_transaction(TEST_DB)
        print(f"3. undo_last_transaction: {undo_msg}")
        assert success is True, f"Undo failed: {undo_msg}"

        # 4. Assert balance is restored to original state and transactions table is empty
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_restored = float(cursor.fetchone()[0])
        cursor.execute("SELECT COUNT(*) FROM transactions")
        tx_count = cursor.fetchone()[0]
        conn.close()
        print(f"4. Restored BCA balance: {bca_restored} (pre-expense: {bca_pre_undo}), transactions count: {tx_count}")
        assert bca_restored == bca_pre_undo, f"Expected {bca_pre_undo}, got {bca_restored}"
        assert tx_count == 0, f"Expected 0 transactions, got {tx_count}"
        print("Part C undo verification PASSED cleanly.")

        # 6. Part D: Feature 7 Persona & Matplotlib Chart Verification
        print("\n[PART D: Feature 7 Persona & Chart Generation Verification]")
        # 1. Verify SYSTEM_PERSONA loaded from persona.md
        print(f"1. Verified SYSTEM_PERSONA length: {len(SYSTEM_PERSONA)} chars")
        assert len(SYSTEM_PERSONA) > 50, "SYSTEM_PERSONA appears empty or unpopulated!"
        if os.path.exists(PERSONA_PATH):
            with open(PERSONA_PATH, "r", encoding="utf-8") as f:
                expected_persona = f.read().strip()
            assert SYSTEM_PERSONA == expected_persona, "SYSTEM_PERSONA does not match persona.md content!"
            print("Loaded SYSTEM_PERSONA matches persona.md content.")

        # 2. Seed sample transactions in test_finance.db: Food (300,000), Utilities (150,000), Impulse (500,000)
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        sample_expenses = [
            ("BCA", "expense", 300000.0, "Food", "Dinner with friends"),
            ("Jenius", "expense", 150000.0, "Utilities", "Electricity bill"),
            ("Cash", "expense", 500000.0, "Impulse", "Gaming keyboard"),
        ]
        cursor.executemany(
            "INSERT INTO transactions (platform, type, amount, category, note) VALUES (?, ?, ?, ?, ?)",
            sample_expenses,
        )
        conn.commit()
        conn.close()
        print("2. Seeded sample expense transactions in test_finance.db.")

        # 3. Call generate_portfolio_chart() with mock breakdown, assert buf.getbuffer().nbytes > 1000
        mock_breakdown = {
            "liquid": {
                "Cash": {"balance": 150000.0, "currency": "IDR"},
                "BCA": {"balance": 2500000.0, "currency": "IDR"},
                "Jenius": {"balance": 750000.0, "currency": "IDR"},
            },
            "investments": {
                "Bibit": 6500000.0,
                "Gotrade": 35.50,
            },
            "total_idr": 9900000.0,
            "total_usd": 35.50,
        }
        p_buf = generate_portfolio_chart(mock_breakdown)
        assert isinstance(p_buf, io.BytesIO), "Expected io.BytesIO buffer"
        p_bytes = p_buf.getbuffer().nbytes
        print(f"3. generate_portfolio_chart() produced {p_bytes} bytes (assert > 1000)")
        assert p_bytes > 1000, f"Portfolio chart buffer too small: {p_bytes} bytes"

        # 4. Call generate_expenses_chart(30, TEST_DB), assert buffer exists and length > 1000
        e_buf, cat_totals = generate_expenses_chart(30, db_name=TEST_DB)
        assert e_buf is not None, "Expected expenses chart buffer to exist"
        e_bytes = e_buf.getbuffer().nbytes
        print(f"4. generate_expenses_chart() produced {e_bytes} bytes, categories: {cat_totals}")
        assert e_bytes > 1000, f"Expenses chart buffer too small: {e_bytes} bytes"
        assert len(cat_totals) == 3, f"Expected 3 expense categories, got {len(cat_totals)}"
        assert cat_totals.get("Impulse") == 500000.0
        assert cat_totals.get("Food") == 300000.0
        assert cat_totals.get("Utilities") == 150000.0

        # 5. Call plt.close('all') and verify no lingering Matplotlib figures
        plt.close("all")
        active_fignums = plt.get_fignums()
        print(f"5. Active Matplotlib figure count: {len(active_fignums)}")
        assert len(active_fignums) == 0, f"Lingering figures detected: {active_fignums}"
        print("Part D persona and chart generation verification PASSED cleanly.")

        # 7. Part E: Feature 8 Database Backup Verification
        print("\n[PART E: Feature 8 Database Backup Verification]")
        # 1. Insert a sample record into test_finance.db
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (platform, type, amount, category, note) VALUES (?, ?, ?, ?, ?)",
            ("BCA", "expense", 77777.0, "TestCategory", "Backup verification note"),
        )
        conn.commit()
        conn.close()
        print("1. Seeded sample record in test_finance.db for backup verification.")

        # 2. Run backup_path = perform_db_backup(TEST_DB)
        backup_path = perform_db_backup(TEST_DB)
        print(f"2. perform_db_backup(TEST_DB) returned: {backup_path}")

        # 3. Verify os.path.exists(backup_path)
        assert os.path.exists(backup_path), f"Backup file does not exist: {backup_path}"
        print(f"3. Verified backup file exists at {backup_path}")

        # 4. Open backup_path with sqlite3 and verify the sample record exists inside the backup file
        backup_conn = sqlite3.connect(backup_path)
        backup_cur = backup_conn.cursor()
        backup_cur.execute("SELECT amount, note FROM transactions WHERE category = 'TestCategory'")
        backup_row = backup_cur.fetchone()
        backup_conn.close()
        assert backup_row is not None, "Sample record not found inside backup database!"
        assert backup_row[0] == 77777.0 and backup_row[1] == "Backup verification note", f"Record mismatch in backup: {backup_row}"
        print(f"4. Verified record inside backup database: {backup_row}")

        # 5. Remove test backup artifacts cleanly
        if os.path.exists(backup_path):
            os.remove(backup_path)

        # 6. Verify check_and_run_biweekly_backup logic
        biweekly_res = check_and_run_biweekly_backup(TEST_DB)
        print(f"6a. Biweekly check initial run: {biweekly_res}")
        assert biweekly_res is not None and os.path.exists(biweekly_res)
        biweekly_second = check_and_run_biweekly_backup(TEST_DB)
        print(f"6b. Biweekly check immediate second run (<14 days): {biweekly_second}")
        assert biweekly_second is None, f"Expected None on second run, got {biweekly_second}"
        if os.path.exists(biweekly_res):
            os.remove(biweekly_res)

        if os.path.exists(BACKUP_DIR) and not os.listdir(BACKUP_DIR):
            try:
                os.rmdir(BACKUP_DIR)
            except Exception:
                pass
        print("5 & 6. Cleaned up test backup files cleanly.")
        print("Part E database backup verification PASSED cleanly.")

        # 8. Part F: Rotating File Logging Verification
        print("\n[PART F: Rotating File Logging Verification]")
        test_log_msg = f"Smoke test log entry at {datetime.now().isoformat()}"
        logger.info(test_log_msg)
        for h in root_logger.handlers:
            h.flush()
        for h in logger.handlers:
            h.flush()

        assert os.path.exists("bot.log"), "bot.log file does not exist!"
        with open("bot.log", "r", encoding="utf-8") as f:
            log_content = f.read()
        assert test_log_msg in log_content, "Test log message not found in bot.log!"
        print(f"Verified logger successfully writes to bot.log ({len(log_content)} bytes).")
        print("Part F logging verification PASSED cleanly.")

        # 9. Clean up test database files
        for f in [TEST_DB, f"{TEST_DB}-wal", f"{TEST_DB}-shm"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        print("Cleaned up test_finance.db and related WAL/SHM files.")
        print("=== ALL TESTS PASSED CLEANLY ===")
    else:
        init_db(DB_NAME)
        if DISCORD_TOKEN:
            logger.info("Starting Discord bot...")
            bot.run(DISCORD_TOKEN)
        else:
            logger.error("DISCORD_TOKEN not found in environment variables.")
