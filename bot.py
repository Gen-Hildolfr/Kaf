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

    prompt = (
        "Extract transaction details from this receipt or financial slip image.\n"
        "Return STRICT JSON ONLY, with no markdown fences, no preamble, and no extra text.\n"
        "Expected JSON schema:\n"
        "{\n"
        '  "kind": "expense" | "income" | "transfer" | "order_filled",\n'
        '  "platform": "BCA" | "Jenius" | "Bibit" | "Gotrade" | "Cash",\n'
        '  "target_platform": "Bibit" | "Gotrade" | "External" | null,\n'
        '  "amount": float or null,\n'
        '  "fee": float or null,\n'
        '  "category": string,\n'
        '  "note": string,\n'
        '  "ticker": "SMMF" | "VTI" | null,\n'
        '  "units_added": float or null\n'
        "}\n"
        "Note for fee: If there is an admin fee or transfer fee (e.g. BIAYA Rp 2,500), put the fee in 'fee' as a float (e.g. 2500.0) and the transfer principal in 'amount' (e.g. 500000.0)."
    )

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

    # Fallback fee detection from note string if fee wasn't in explicit field
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

    elif kind == "transfer":
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
            VALUES (?, 'transfer', ?, 'Transfer', ?)
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
            title="🔁 Transfer Recorded",
            color=discord.Color.blue()
        )
        embed.add_field(name="From", value=platform, inline=True)
        embed.add_field(name="To", value=target_platform, inline=True)
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
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="📈 Order Filled / Units Added",
            color=discord.Color.purple()
        )
        embed.add_field(name="Platform", value=platform, inline=True)
        embed.add_field(name="Ticker", value=ticker, inline=True)
        embed.add_field(name="Units Added", value=f"{units_added:,.4f}", inline=True)
        embed.add_field(name="Total Units", value=f"{new_units:,.4f}", inline=False)
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
                    await message.reply("Sorry, I encountered an error talking to the AI service.")
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
                # Run vision query in background worker thread to keep asyncio event loop & Discord heartbeats responsive
                data = await asyncio.to_thread(query_unsloth_vision, image_bytes)
                embed = process_parsed_slip(data)
                await message.reply(embed=embed)
            except Exception as e:
                print(f"Error processing image slip: {e}")
                traceback.print_exc()
                await message.reply(
                    "❌ Unable to parse this receipt or financial slip. "
                    "Please ensure the image is clear and contains transaction details."
                )
    else:
        # Attachment is not an image; fall back to text chat if message text is present
        if message.content.strip():
            async with message.channel.typing():
                try:
                    reply_text = await asyncio.to_thread(query_unsloth_chat, message.content)
                    await message.reply(reply_text)
                except Exception as e:
                    print(f"Chat error: {e}")
                    traceback.print_exc()
                    await message.reply("Sorry, I encountered an error talking to the AI service.")


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

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="addcash", description="Record cash inflow/income to your physical wallet")
@app_commands.describe(amount="Amount of cash added in IDR", note="Description or source of cash")
async def addcash(interaction: discord.Interaction, amount: float, note: str):
    new_balance = record_cash_flow(amount, "income", note)
    embed = discord.Embed(
        title="💵 Cash Inflow Added",
        color=discord.Color.green()
    )
    embed.add_field(name="Amount Added", value=f"Rp {amount:,.2f}", inline=True)
    embed.add_field(name="Note", value=note, inline=True)
    embed.add_field(name="Updated Wallet Total", value=f"Rp {new_balance:,.2f}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="usecash", description="Record cash outflow/expense from your physical wallet")
@app_commands.describe(amount="Amount of cash spent in IDR", note="Description or purpose of expense")
async def usecash(interaction: discord.Interaction, amount: float, note: str):
    new_balance = record_cash_flow(amount, "expense", note)
    embed = discord.Embed(
        title="💸 Cash Outflow Logged",
        color=discord.Color.red()
    )
    embed.add_field(name="Amount Spent", value=f"Rp {amount:,.2f}", inline=True)
    embed.add_field(name="Note", value=note, inline=True)
    embed.add_field(name="Remaining Wallet Balance", value=f"Rp {new_balance:,.2f}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="checkexpenses", description="List recent expenses from the last 7 days")
async def checkexpenses(interaction: discord.Interaction):
    rows, total_amount = get_recent_summary("expense", 7)
    embed = discord.Embed(
        title="📊 Expenses (Last 7 Days)",
        color=discord.Color.gold()
    )
    if rows:
        lines = []
        for r in rows[:15]:
            date_str, platform, amount, note = r[0], r[1], float(r[2]), r[3]
            lines.append(f"• `{date_str}` | **{platform}** | Rp {amount:,.2f} - *{note}*")
        embed.description = "\n".join(lines)
    else:
        embed.description = "No expenses recorded in the last 7 days."

    embed.add_field(name="Total (7 Days)", value=f"Rp {total_amount:,.2f}", inline=False)
    await interaction.response.send_message(embed=embed)


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
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------
# Main Entry Point & Smoke Test
# ---------------------------------------------------------
if __name__ == "__main__":
    init_db()

    if "--test" in sys.argv:
        print("=== RUNNING CLI SMOKE TEST (--test) ===")
        
        # Part A: Feature 3 Verification
        print("\n[PART A: Feature 3 Verification]")
        cash_after_income = record_cash_flow(100000, "income", "ATM withdrawal")
        print(f"1. record_cash_flow(100000, 'income', 'ATM withdrawal') -> Cash Balance: {cash_after_income}")

        cash_after_expense = record_cash_flow(25000, "expense", "Lunch")
        print(f"2. record_cash_flow(25000, 'expense', 'Lunch') -> Cash Balance: {cash_after_expense}")

        breakdown = get_balance_breakdown()
        print(f"3. get_balance_breakdown() -> {breakdown}")

        expense_rows, expense_total = get_recent_summary("expense", 7)
        print(f"4. get_recent_summary('expense', 7) -> Total: {expense_total}, Rows count: {len(expense_rows)}")

        assert "liquid" in breakdown
        assert "investments" in breakdown
        assert isinstance(breakdown["liquid"]["Cash"]["balance"], (int, float))
        assert isinstance(breakdown["investments"].get("Bibit"), (int, float))
        assert isinstance(breakdown["investments"].get("Gotrade"), (int, float))
        print("Part A checks PASSED.")

        # Part B: Feature 4 Database Routing Verification with 4 Mocks
        print("\n[PART B: Feature 4 Database Routing Verification]")
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Jenius'")
        jenius_start = float(cursor.fetchone()[0])
        cursor.execute("SELECT units FROM holdings WHERE platform = 'Bibit' AND ticker = 'SMMF'")
        smmf_start = float(cursor.fetchone()[0])
        conn.close()

        # 1. Mock "expense" (BCA Rp 50.000 for groceries)
        mock_expense = {
            "kind": "expense",
            "platform": "BCA",
            "target_platform": None,
            "amount": 50000.0,
            "category": "Groceries",
            "note": "Supermarket groceries",
            "ticker": None,
            "units_added": None
        }
        embed_expense = process_parsed_slip(mock_expense)
        assert isinstance(embed_expense, discord.Embed)
        print("1. Mock Expense (BCA -50,000) processed -> Red Embed")

        # 2. Mock "transfer" with 2,500 admin fee (BCA Rp 500.000 to External + Rp 2.500 fee)
        raw_transfer_text_with_nbsp = """{\r
\xa0 "kind": "transfer",\r
\xa0 "platform": "BCA",\r
\xa0 "target_platform": "External",\r
\xa0 "amount": 500000.0,\r
\xa0 "fee": 2500.0,\r
\xa0 "category": "Transfer",\r
\xa0 "note": "m-Transfer BERHASIL to BANK BRI 119801002584539 FIONNA CALYSTA TIKHI, BIAYA Rp 2,500.00, Ref 950312026091623221451299A21D4F5F05D",\r
\xa0 "ticker": null,\r
\xa0 "units_added": null\r
}"""
        mock_transfer = clean_and_parse_json(raw_transfer_text_with_nbsp)
        embed_transfer = process_parsed_slip(mock_transfer)
        assert isinstance(embed_transfer, discord.Embed)
        print("2. Mock Transfer with NBSP & Fee (BCA -500,000 + 2,500 fee) parsed & processed -> Blue Embed")

        # 3. Mock "order_filled" (Bibit SMMF +251.05 units)
        mock_order = {
            "kind": "order_filled",
            "platform": "Bibit",
            "target_platform": None,
            "amount": 500000.0,
            "category": "Investment",
            "note": "Bibit SMMF order executed",
            "ticker": "SMMF",
            "units_added": 251.05
        }
        embed_order = process_parsed_slip(mock_order)
        assert isinstance(embed_order, discord.Embed)
        print("3. Mock Order Filled (Bibit SMMF +251.05 units) processed -> Purple Embed")

        # 4. Mock "income" (Jenius Rp 1.000.000 freelance)
        mock_income = {
            "kind": "income",
            "platform": "Jenius",
            "target_platform": None,
            "amount": 1000000.0,
            "category": "Salary",
            "note": "Freelance design payment",
            "ticker": None,
            "units_added": None
        }
        embed_income = process_parsed_slip(mock_income)
        assert isinstance(embed_income, discord.Embed)
        print("4. Mock Income (Jenius +1,000,000) processed -> Green Embed")

        # Verify DB Changes
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'BCA'")
        bca_end = float(cursor.fetchone()[0])
        cursor.execute("SELECT balance FROM accounts WHERE platform = 'Jenius'")
        jenius_end = float(cursor.fetchone()[0])
        cursor.execute("SELECT units FROM holdings WHERE platform = 'Bibit' AND ticker = 'SMMF'")
        smmf_end = float(cursor.fetchone()[0])

        cursor.execute("SELECT type, amount, category, note FROM transactions WHERE platform = 'BCA'")
        bca_txs = cursor.fetchall()
        cursor.execute("SELECT type, amount, category, note FROM transactions WHERE platform = 'Jenius'")
        jenius_txs = cursor.fetchall()
        conn.close()

        print("\n--- Verifying Database Assertions ---")
        bca_diff = bca_end - bca_start
        jenius_diff = jenius_end - jenius_start
        smmf_diff = smmf_end - smmf_start

        # BCA should decrement by: 50,000 (expense) + 500,000 (transfer) + 2,500 (fee) = 552,500
        print(f"BCA: start={bca_start}, end={bca_end}, diff={bca_diff} (expected -552500.0)")
        assert round(bca_diff, 2) == -552500.0, f"BCA decrement failed: {bca_diff}"

        print(f"Jenius: start={jenius_start}, end={jenius_end}, diff={jenius_diff} (expected +1000000.0)")
        assert round(jenius_diff, 2) == 1000000.0, f"Jenius increment failed: {jenius_diff}"

        print(f"SMMF Units: start={smmf_start}, end={smmf_end}, diff={smmf_diff} (expected +251.05)")
        assert round(smmf_diff, 4) == 251.05, f"SMMF units increment failed: {smmf_diff}"

        recorded_types = [tx[0] for tx in bca_txs] + [tx[0] for tx in jenius_txs]
        recorded_categories = [tx[2] for tx in bca_txs]
        print(f"Recorded Transaction Types: {recorded_types}")
        print(f"Recorded BCA Categories: {recorded_categories}")
        assert "expense" in recorded_types, "Missing 'expense' transaction"
        assert "transfer" in recorded_types, "Missing 'transfer' transaction"
        assert "income" in recorded_types, "Missing 'income' transaction"
        assert "Fee" in recorded_categories, "Missing 'Fee' category in BCA transactions"

        print("\nAll 4 transaction routing paths and fee logging verified successfully!")
        print("=== TEST PASSED CLEANLY ===")
    else:
        if DISCORD_TOKEN:
            print("Starting Discord bot...")
            bot.run(DISCORD_TOKEN)
        else:
            print("ERROR: DISCORD_TOKEN not found in environment variables.")
