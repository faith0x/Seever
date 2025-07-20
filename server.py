from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import time
from datetime import datetime
import json
import os
import logging

app = Flask(__name__)
scheduler = BackgroundScheduler()

# Configuration
API_KEY = '3fb2ca0a-a738-40d2-8d33-cc217f0dd514'  # Your Solana Tracker API key
TARGET_WALLET = 'AJKgkQyHQBMK8MVkoKfp7qZo3VUMLiLszZV2WM9BhJgF'  # Your target wallet
MY_INITIAL_SOL = 2.0
BUY_PERCENTAGE = 0.1  # Use 10% of current SOL balance per buy
SELL_PERCENTAGE = 0.5  # Sell 50% of holdings per sell
TRADES_FILE = 'trades.json'  # Store tracked wallet trades
LOG_FILE = 'trading.log'  # Log file for simulated wallet

# Simulated wallet state
my_sol_balance = MY_INITIAL_SOL
my_token_holdings = {}  # {token_address: {'amount': float, 'buy_price_usd': float, 'buy_time': int, 'total_bought_usd': float, 'total_sold_usd': float, 'pnl_usd': float, 'sold_time': int or None, 'name': str}}
last_processed_timestamp = int(time.time() * 1000)  # Start from current time in milliseconds
tracked_trades = []  # Store all trades for the tracked wallet

# Setup logging
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')

# Load or save trades to file
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    return []

def save_trades():
    with open(TRADES_FILE, 'w') as f:
        json.dump(tracked_trades, f, indent=2)

# Fetch trades from Solana Tracker API
def fetch_trades():
    url = f'https://data.solanatracker.io/wallet/{TARGET_WALLET}/trades'
    headers = {'accept': 'application/json', 'X-API-KEY': API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()['trades']
    else:
        logging.error(f"Failed to fetch trades: {response.status_code}")
        return []

# Fetch current price of a token
def fetch_current_price(token_address):
    url = f'https://data.solanatracker.io/price?token={token_address}'
    headers = {'accept': 'application/json', 'X-API-KEY': API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()['price']
    logging.error(f"Failed to fetch price for {token_address}: {response.status_code}")
    return None

# Simulate trades based on target wallet
def simulate_trades():
    global my_sol_balance, my_token_holdings, last_processed_timestamp, tracked_trades
    trades = fetch_trades()
    # Filter new trades
    new_trades = [trade for trade in trades if trade['time'] > last_processed_timestamp]
    # Sort by timestamp for chronological processing
    new_trades = sorted(new_trades, key=lambda x: x['time'])
    # Add new trades to tracked_trades
    tracked_trades.extend(new_trades)
    save_trades()
    
    for trade in new_trades:
        timestamp = trade['time']
        if trade['from']['token']['symbol'] == 'SOL':
            # Buy trade
            token_address = trade['to']['address']
            token_name = trade['to']['token']['name']
            buy_amount_sol = my_sol_balance * BUY_PERCENTAGE
            if my_sol_balance >= buy_amount_sol:
                buy_price_usd = trade['price']['usd']
                amount_bought = buy_amount_sol / buy_price_usd
                if token_address not in my_token_holdings:
                    my_token_holdings[token_address] = {
                        'amount': amount_bought,
                        'buy_price_usd': buy_price_usd,
                        'buy_time': timestamp,
                        'total_bought_usd': buy_amount_sol * buy_price_usd,
                        'total_sold_usd': 0.0,
                        'pnl_usd': 0.0,
                        'sold_time': None,
                        'name': token_name
                    }
                else:
                    holding = my_token_holdings[token_address]
                    total_amount = holding['amount'] + amount_bought
                    total_cost = holding['total_bought_usd'] + (buy_amount_sol * buy_price_usd)
                    holding['amount'] = total_amount
                    holding['buy_price_usd'] = total_cost / total_amount
                    holding['total_bought_usd'] = total_cost
                my_sol_balance -= buy_amount_sol
                logging.info(f"Bought {buy_amount_sol:.4f} SOL of {token_name} ({token_address}), SOL balance: {my_sol_balance:.4f}")
        elif trade['to']['token']['symbol'] == 'SOL':
            # Sell trade
            token_address = trade['from']['address']
            token_name = trade['from']['token']['name']
            if token_address in my_token_holdings:
                holding = my_token_holdings[token_address]
                if holding['amount'] > 0:
                    sell_amount = holding['amount'] * SELL_PERCENTAGE
                    sell_price_usd = trade['price']['usd']
                    sell_value_sol = sell_amount * sell_price_usd / (fetch_current_price('So11111111111111111111111111111111111111112') or 1)
                    pnl_usd = (sell_price_usd - holding['buy_price_usd']) * sell_amount
                    holding['amount'] -= sell_amount
                    holding['total_sold_usd'] += sell_amount * sell_price_usd
                    holding['pnl_usd'] += pnl_usd
                    if holding['amount'] <= 0:
                        holding['amount'] = 0
                        holding['sold_time'] = timestamp
                    my_sol_balance += sell_value_sol
                    logging.info(f"Sold {sell_amount:.2f} of {token_name} ({token_address}) for {sell_value_sol:.4f} SOL, PNL: {pnl_usd:.2f} USD, SOL balance: {my_sol_balance:.4f}")
    
    # Log PNL for holdings
    sol_price_usd = fetch_current_price('So11111111111111111111111111111111111111112')
    for token_address, holding in my_token_holdings.items():
        if holding['amount'] > 0:
            current_price_usd = fetch_current_price(token_address) or holding['buy_price_usd']
            unrealized_pnl = (current_price_usd - holding['buy_price_usd']) * holding['amount']
            logging.info(f"Unrealized PNL for {holding['name']} ({token_address}): {unrealized_pnl:.2f} USD, {unrealized_pnl / sol_price_usd:.4f} SOL")
    
    if new_trades:
        last_processed_timestamp = max([trade['time'] for trade in new_trades])
        logging.info(f"Processed {len(new_trades)} new trades, last timestamp: {last_processed_timestamp}")

# Calculate holding time
def calculate_holding_time(buy_time, sold_time=None):
    buy_datetime = datetime.fromtimestamp(buy_time / 1000)
    end_datetime = datetime.fromtimestamp(sold_time / 1000) if sold_time else datetime.now()
    delta = end_datetime - buy_datetime
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days}d {hours}h"

# Group trades by token for status endpoint
def group_trades_by_token():
    token_trades = {}
    for trade in tracked_trades:
        token_address = trade['to']['address'] if trade['from']['token']['symbol'] == 'SOL' else trade['from']['address']
        if token_address not in token_trades:
            token_trades[token_address] = []
        token_trades[token_address].append(trade)
    return token_trades

# Get status for tracked wallet
def get_status():
    status = []
    sol_price_usd = fetch_current_price('So11111111111111111111111111111111111111112')
    for token_address, trades in group_trades_by_token().items():
        total_bought_usd = sum(t['volume']['usd'] for t in trades if t['from']['token']['symbol'] == 'SOL')
        total_sold_usd = sum(t['volume']['usd'] for t in trades if t['to']['token']['symbol'] == 'SOL')
        amount_held = sum(t['to']['amount'] for t in trades if t['from']['token']['symbol'] == 'SOL') - sum(t['from']['amount'] for t in trades if t['to']['token']['symbol'] == 'SOL')
        first_buy_time = min(t['time'] for t in trades if t['from']['token']['symbol'] == 'SOL')
        last_sell_time = max((t['time'] for t in trades if t['to']['token']['symbol'] == 'SOL'), default=None)
        current_price_usd = fetch_current_price(token_address) or trades[0]['price']['usd']
        avg_buy_price = total_bought_usd / sum(t['to']['amount'] for t in trades if t['from']['token']['symbol'] == 'SOL') if total_bought_usd else 0
        realized_pnl = sum((t['price']['usd'] - avg_buy_price) * t['from']['amount'] for t in trades if t['to']['token']['symbol'] == 'SOL')
        unrealized_pnl = (current_price_usd - avg_buy_price) * amount_held if amount_held > 0 else 0
        status_entry = {
            "contract_address": token_address,
            "name": trades[0]['to']['token']['name'] if trades[0]['from']['token']['symbol'] == 'SOL' else trades[0]['from']['token']['name'],
            "status": "sold" if amount_held <= 0 else "holding",
            "total_bought_usd": round(total_bought_usd, 2),
            "total_sold_usd": round(total_sold_usd, 2) if total_sold_usd > 0 else "none",
            "pnl_usd": round(realized_pnl if amount_held <= 0 else unrealized_pnl, 2),
            "sol_value": round(realized_pnl / sol_price_usd, 4) if sol_price_usd and amount_held <= 0 else round(unrealized_pnl / sol_price_usd, 4) if sol_price_usd else "N/A",
            "held_time": calculate_holding_time(first_buy_time, last_sell_time if amount_held <= 0 else None)
        }
        status.append(status_entry)
    return {
        "portfolio": status,
        "simulated_sol_balance": round(my_sol_balance, 4),
        "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    
@app.route('/status', methods=['GET'])
def status():
    return jsonify(get_status())

if __name__ == '__main__':
    tracked_trades = load_trades()
    scheduler.add_job(simulate_trades, 'interval', minutes=3)
    scheduler.start()
    app.run(host='0.0.0.0', port=5000)
