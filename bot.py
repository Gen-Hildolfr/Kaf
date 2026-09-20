import os
import sys
import re
import json
import base64
import asyncio
import sqlite3
import traceback
import requests
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yfinance as yf

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


def get_balance_breakdown(db_name: str = DB_NAME) -> dict:
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT platform, balance, currency FROM accounts WHERE platform IN ('Cash', 'BCA', 'Jenius')"
    )
    account_rows = cursor.fetchall()
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
    total_idr = total_liquid_idr + investments.get("Bibit", 0.0)
    total_usd = investments.get("Gotrade", 0.0)

    return {
        "liquid": liquid,
        "investments": investments,
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


def query_unsloth_chat(prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    if UNSLOTH_API_KEY:
        headers["Authorization"] = f"Bearer {UNSLOTH_API_KEY}"

    payload = {
        "model": UNSLOTH_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }
    response = requests.post(UNSLOTH_API_URL, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    reply = data["choices"][0]["message"]["content"]
    if "<think>" in reply and "</think>" in reply:
        reply = reply.split("</think>", 1)[1].strip()
    return reply


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
- For any transfer or switching, format the note strictly as: "transfer to <bank name> <recipient name>" (e.g. "transfer to BCA {USER_NAME}" or "transfer to BANK BRI FIONNA CALYSTA TIKHI" or "transfer to BIBIT RDN {USER_NAME}").

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


# ---------------------------------------------------------
# Discord Bot Setup & Event Listeners
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


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
                    print(f"Chat error: {e}")
                    traceback.print_exc()
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
                embed = process_parsed_slip(data)
            except Exception as e:
                print(f"Error extracting or processing receipt slip: {e}")
                traceback.print_exc()
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
                    print(f"Error: Bot lacks permission to send messages in channel {message.channel.id}")
            except Exception as e:
                print(f"Error sending embed response: {e}")
                traceback.print_exc()
    else:
        if message.content.strip():
            async with message.channel.typing():
                try:
                    reply_text = await asyncio.to_thread(query_unsloth_chat, message.content)
                    await message.reply(reply_text)
                except Exception as e:
                    print(f"Chat error: {e}")
                    traceback.print_exc()
                    try:
                        await message.reply("Sorry, I encountered an error talking to the AI service.")
                    except Exception:
                        pass


# ---------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------
@bot.tree.command(name="checkbalance", description="View portfolio and balance breakdown across all accounts")
async def checkbalance(interaction: discord.Interaction):
    await interaction.response.defer()
    breakdown = get_balance_breakdown()

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
    gotrade_val = investments.get("Gotrade", 0.0)

    investments_text = (
        f"• **Bibit (SMMF)**: Rp {bibit_val:,.2f} IDR\n"
        f"• **Gotrade (VTI)**: ${gotrade_val:,.2f} USD"
    )
    embed.add_field(name="📈 Investments", value=investments_text, inline=False)

    total_idr = breakdown.get("total_idr", total_liquid + bibit_val)
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


# ---------------------------------------------------------
# Main Entry Point & Smoke Test
# ---------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    init_db()

    if "--test" in sys.argv:
        print("=== RUNNING CLI SMOKE TEST (--test) ===")
        
        # Part A: Feature 3 Verification
        print("\n[PART A: Feature 3 Cash Flow]")
        cash_after_income = record_cash_flow(100000, "income", "ATM withdrawal")
        print(f"1. Cash balance after income: {cash_after_income}")

        cash_after_expense = record_cash_flow(25000, "expense", "Lunch")
        print(f"2. Cash balance after expense: {cash_after_expense}")

        breakdown = get_balance_breakdown()
        print(f"3. Breakdown: {breakdown}")

        outflow_rows, outflow_total = get_recent_outflows(7)
        print(f"4. Total Outflows: {outflow_total}, Count: {len(outflow_rows)}")
        print("Part A checks PASSED.")

        # Part B: Feature 5 RDN Routing, Order Execution & Activity Verification
        print("\n[PART B: Feature 5 Verification]")
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Bibit'")
        bibit_cash_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT units FROM holdings WHERE platform = 'Bibit' AND ticker = 'SMMF'")
        smmf_units_start = float(cursor.fetchone()[0])
        conn.close()

        # 1. RDN Transfer: BCA -> Bibit Rp 1.000.000
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
        embed_rdn = process_parsed_slip(mock_rdn)
        assert isinstance(embed_rdn, discord.Embed)

        conn = sqlite3.connect(DB_NAME)
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

        # 2. Order Filled: Bibit purchase of 251.05 SMMF units for Rp 500.000
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
        embed_order = process_parsed_slip(mock_order)
        assert isinstance(embed_order, discord.Embed)

        conn = sqlite3.connect(DB_NAME)
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

        # 3. Command verification: Call get_recent_activity(days=14)
        act_rows, act_outflows, act_switched = get_recent_activity(days=14)
        print(f"3. get_recent_activity(days=14): {len(act_rows)} items, Outflow=Rp {act_outflows:,.2f}, Switched=Rp {act_switched:,.2f}")
        assert isinstance(act_rows, list)
        assert isinstance(act_outflows, float)
        assert isinstance(act_switched, float)
        notes = [r[5] for r in act_rows]
        assert f"transfer to BIBIT RDN {USER_NAME}" in notes, "Missing RDN transfer in recent activity!"

        print("=== ALL TESTS PASSED CLEANLY ===")
    else:
        if DISCORD_TOKEN:
            print("Starting Discord bot...")
            bot.run(DISCORD_TOKEN)
        else:
            print("ERROR: DISCORD_TOKEN not found in environment variables.")
