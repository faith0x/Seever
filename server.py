from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import time
from datetime import datetime, timedelta
import json
import os
import logging
from pymongo import MongoClient

price_cache = {}  # {token_address: (price, timestamp)}
CACHE_EXPIRY = 300  # 5 minutes in seconds

app = Flask(__name__)
scheduler = BackgroundScheduler()

# Configuration
API_KEY = '3fb2ca0a-a738-40d2-8d33-cc217f0dd514'  # Your Solana Tracker API key
TARGET_WALLET = 'AJKgkQyHQBMK8MVkoKfp7qZo3VUMLiLszZV2WM9BhJgF'  # Your target wallet
MY_INITIAL_SOL = 2.0
BUY_PERCENTAGE = 0.1  # Use 10% of current SOL balance per buy
SELL_PERCENTAGE = 0.5  # Sell 50% of holdings per sell
LOG_FILE = 'trading.log'  # Log file for local testing
ENV = os.getenv('ENV', 'local')  # 'local' or 'render'

# MongoDB Configuration
MONGO_URI = 'your_mongo_uri'  # Replace with your MongoDB URI
client = MongoClient(MONGO_URI)
db = client['trade_database']
trades_collection = db['trades']

# Simulated wallet state
my_sol_balance = MY_INITIAL_SOL
my_token_holdings = {}  # {token_address: {'amount': float, 'buy_price_usd': float, 'buy_time': int, 'total_bought_usd': float, 'total_sold_usd': float, 'packed': float, 'sold_time': int or None, 'name': str}}
last_processed_timestamp = int((datetime.now() - timedelta(hours=5)).timestamp() * 1000)

# Setup logging based on environment
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(message)s')

if ENV == 'local':
    # Local: Log to file and console with full details
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
else:
    # Render: Log to console only, limited details
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# Fetch trades from Solana Tracker API
def fetch_trades(min_timestamp):
    url = f'https://data.solanatracker.io/wallet/{TARGET_WALLET}/trades'
    headers = {'accept': 'application/json', 'X-API-KEY': API_KEY}
    all_trades = []
    cursor = None
    while True:
        response = requests.get(url + (f"?cursor={cursor}" if cursor else ""), headers=headers)
        if response.status_code != 200:
            logging.error(f"Failed to fetch trades: {response.status_code}")
            break
        data = response.json()
        trades = [t for t in data['trades'] if t['time'] >= min_timestamp]
        all_trades.extend(trades)
        if not data['hasNextPage'] or not trades:
            break
        cursor = data['nextCursor']
    
    fetch_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if all_trades:
        logging.info(f"Successfully fetched {len(all_trades)} trades at {fetch_time}")
        if ENV == 'local':
            for trade in all_trades:
                logging.info(f"Fetched trade: {json.dumps(trade, indent=2)}")
    else:
        logging.info(f"No new trades fetched at {fetch_time}")
    
    return all_trades

# Fetch current price of a token
# Fetch current price of a token with caching and retries
def fetch_current_price(token_address):
    current_time = time.time()
    # Check cache first
    if token_address in price_cache:
        price, timestamp = price_cache[token_address]
        if current_time - timestamp < CACHE_EXPIRY:
            logging.info(f"Using cached price for {token_address}: {price}")
            return price
    
    # Fetch from API with retry logic
    url = f'https://data.solanatracker.io/price?token={token_address}'
    headers = {'accept': 'application/json', 'X-API-KEY': API_KEY}
    max_retries = 3
    for attempt in range(max_retries):
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            price = response.json()['price']
            price_cache[token_address] = (price, current_time)
            logging.info(f"Fetched new price for {token_address}: {price}")
            return price
        elif response.status_code == 429:
            logging.warning(f"Rate limit hit for {token_address}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
            continue
        else:
            logging.error(f"Failed to fetch price for {token_address}: {response.status_code}")
            break
    logging.error(f"All retries failed for {token_address}, using fallback")
    return None  # Fallback to None if all retries fail

# Simulate trades based on target wallet
def simulate_trades():
    global my_sol_balance, my_token_holdings, last_processed_timestamp
    trades = fetch_trades(last_processed_timestamp)
    new_trades = [trade for trade in trades if trade['time'] > last_processed_timestamp]
    new_trades = sorted(new_trades, key=lambda x: x['time'])
    
    # Store new trades in MongoDB
    if new_trades:
        trades_collection.insert_many(new_trades)
    
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
            # Rest of the sell logic...
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
def group_trades_by_token(trades):
    token_trades = {}
    for trade in trades:
        token_address = trade['to']['address'] if trade['from']['token']['symbol'] == 'SOL' else trade['from']['address']
        if token_address not in token_trades:
            token_trades[token_address] = []
        token_trades[token_address].append(trade)
    return token_trades

# Get status for tracked wallet
def get_status():
    # Fetch all trades from MongoDB since last_processed_timestamp
    tracked_trades = list(trades_collection.find({"time": {"$gte": int((datetime.now() - timedelta(hours=14)).timestamp() * 1000)}}))
    status = []
    sol_price_usd = fetch_current_price('So11111111111111111111111111111111111111112')
    for token_address, trades in group_trades_by_token(tracked_trades).items():
        total_bought_usd = sum(t['volume']['usd'] for t in trades if t['from']['token']['symbol'] == 'SOL')
        total_sold_usd = sum(t['volume']['usd'] for t in trades if t['to']['token']['symbol'] == 'SOL')
        amount_held = sum(t['to']['amount'] for t in trades if t['from']['token']['symbol'] == 'SOL') - sum(t['from']['amount'] for t in trades if t['to']['token']['symbol'] == 'SOL')
        if amount_held <= 0 and total_sold_usd == 0:
            continue  # Skip fully sold trades with no activity
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
    return {"portfolio": status, "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")}

@app.route('/status', methods=['GET'])
def status():
    return jsonify(get_status())

if __name__ == '__main__':
    logging.info("Starting the application...")
    scheduler.add_job(simulate_trades, 'interval', minutes=3, next_run_time=datetime.now())
    scheduler.start()
    app.run(host='0.0.0.0', port=5000)
