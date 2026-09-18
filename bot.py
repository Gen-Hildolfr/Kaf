import os
import sys
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yfinance as yf

# ---------------------------------------------------------
# Configuration & Database Constants
# ---------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DB_NAME = "finance.db"

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
# Discord Bot Setup & Slash Commands
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
        print("=== RUNNING FEATURE 3 SMOKE TEST (--test) ===")
        # 1. Calls record_cash_flow(100000, 'income', 'ATM withdrawal')
        cash_after_income = record_cash_flow(100000, "income", "ATM withdrawal")
        print(f"1. record_cash_flow(100000, 'income', 'ATM withdrawal') -> New Cash Balance: {cash_after_income}")

        # 2. Calls record_cash_flow(25000, 'expense', 'Lunch')
        cash_after_expense = record_cash_flow(25000, "expense", "Lunch")
        print(f"2. record_cash_flow(25000, 'expense', 'Lunch') -> New Cash Balance: {cash_after_expense}")

        # 3. Calls get_balance_breakdown()
        breakdown = get_balance_breakdown()
        print(f"3. get_balance_breakdown() -> {breakdown}")

        # 4. Calls get_recent_summary('expense', 7)
        expense_rows, expense_total = get_recent_summary("expense", 7)
        print(f"4. get_recent_summary('expense', 7) -> Total: {expense_total}, Rows count: {len(expense_rows)}")
        for r in expense_rows:
            print(f"   Row: {r}")

        # 5. Validates that the returned data structures contain valid keys and numeric balances.
        assert "liquid" in breakdown, "Missing 'liquid' key in breakdown"
        assert "investments" in breakdown, "Missing 'investments' key in breakdown"
        assert "Cash" in breakdown["liquid"], "Missing 'Cash' in breakdown['liquid']"
        assert "BCA" in breakdown["liquid"], "Missing 'BCA' in breakdown['liquid']"
        assert "Jenius" in breakdown["liquid"], "Missing 'Jenius' in breakdown['liquid']"
        assert isinstance(breakdown["liquid"]["Cash"]["balance"], (int, float)), "Cash balance is not numeric"
        assert isinstance(breakdown["investments"].get("Bibit"), (int, float)), "Bibit total is not numeric"
        assert isinstance(breakdown["investments"].get("Gotrade"), (int, float)), "Gotrade total is not numeric"
        assert isinstance(expense_total, (int, float)), "expense_total is not numeric"
        assert expense_total >= 25000.0, "expense_total should include at least 25000.0"
        assert len(expense_rows) >= 1, "recent expenses rows should not be empty"

        print("5. Validation: All keys present and numeric balances verified!")
        print("=== TEST PASSED SUCCESSFULLY ===")
    else:
        if DISCORD_TOKEN:
            print("Starting Discord bot...")
            bot.run(DISCORD_TOKEN)
        else:
            print("ERROR: DISCORD_TOKEN not found in environment variables.")
