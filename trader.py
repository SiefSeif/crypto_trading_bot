from flask import Flask, request, jsonify, abort
from datetime import datetime
from dotenv import load_dotenv
import os
import json
import uuid 
import sys
import time

from coinbase.rest import RESTClient

load_dotenv()  # loads .env from current directory

app = Flask(__name__)

# Load variables
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv('API_SECRET')
SECRET_TOKEN = os.getenv('SECRET_TOKEN')
MINIMUM_ACT_PRICE = 0

client = RESTClient(api_key=API_KEY, api_secret=API_SECRET)

# Configuration
AMOUNTS = []
SIDE = 'SELL'  # Default SIDE
FEES_ESTIMATE = 0.006  # 0.6% pessimistic estimate
SLIPPAGE_ESTIMATE = 10
MIN_PROFIT = 0
CHECK_PREV_BUY = False
STATE_FILE = None



def get_state_file(currency: str):
    """Get state file path based on main currency"""
    return f"/home/ubuntu/workspace/crypto_trading_bot/state_{currency}.json"

def load_state(currency : str):
    state_file = get_state_file(currency)
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            content = f.read()
            if content:
                return json.loads(content)
    
    # Return default if file doesn't exist
    return {
        'position': None,
        'last_sell_price': None,
        'last_buy_price': None,
        'last_sell_gain': 0.0,
        'last_buy_pay': 0.0,
        'total_profit': 0.0,
    }

def save_state(trading_state, currency : str):
    state_file = get_state_file(currency)
    with open(state_file, "w") as f:
        json.dump(trading_state, f)

# Trading Logic
def execute_first_order(signal_data):
    """Execute SELL or BUY order"""
    
    price = signal_data.get('price')
    product_id = signal_data.get('ticker') # customized BTC-GBP, normally ticker is BTCGBP
    currency = product_id[:3]

    global SIDE

    print(f"\n{'='*60}")
    print(f"🔴 EXECUTING ${SIDE} ORDER")
    print(f"{'='*60}")
    
    try:
        # Place limit order on Coinbase
        # if SIDE == 'SELL':
        #     order = client.market_order_sell(client_order_id=str(uuid.uuid4()), product_id=product_id, base_size=AMOUNTS[currency])
        # elif SIDE == 'BUY':
        #     order = client.market_order_buy(client_order_id=str(uuid.uuid4()), product_id=product_id, base_size=AMOUNTS[currency])
        
        # Place limit order on Coinbase
        if SIDE == 'SELL':
            if MINIMUM_ACT_PRICE != 0 and price < MINIMUM_ACT_PRICE:
                print(f"⚠️ Current price ${price} is below minimum activation price ${MINIMUM_ACT_PRICE}. Skipping SELL order.")
                return False
            order_response = client.limit_order_gtc_sell(client_order_id=str(uuid.uuid4()), product_id=product_id,\
                                                base_size=AMOUNTS[currency], limit_price=str(round(price * 0.999, 2)))
        elif SIDE == 'BUY':

            if MINIMUM_ACT_PRICE != 0 and price > MINIMUM_ACT_PRICE:
                print(f"⚠️ Current price ${price} is above maximum activation price ${MINIMUM_ACT_PRICE}. Skipping BUY order.")
                return False
            order_response = client.limit_order_gtc_buy(client_order_id=str(uuid.uuid4()), product_id=product_id,\
                                                base_size=AMOUNTS[currency], limit_price=str(round(price * 1.001, 2)))
        
        if order_response.success:

            order_id = order_response.success_response["order_id"]

            # NOW wait for it to be FILLED
            for i in range(150):  # 300 seconds
                time.sleep(2) # check every 2 seconds
                
                get_response = client.get_order(order_id)  # Returns GetOrderResponse
                order = get_response.order  # Order object

                if order.status == "FILLED":

                    print("✅ Order FILLED!")
                    # get executed price
                    execution_price = round(float(order.average_filled_price), 2)
                    fees = round(float(order.total_fees), 2)

                    trading_state = load_state(currency)
                    
                    # Update state
                    if SIDE == 'SELL':
                        gain_before_fees = round(float(order.filled_value), 2)
                        gain_after_fees = round(float(order.total_value_after_fees), 2)
                        trading_state['last_sell_gain'] = gain_after_fees
                        trading_state['position'] = 'waiting_buy'
                        
                        print(f"✅ ${SIDE} executed at ${execution_price}")
                        print(f"💸 Fees: ${fees}")
                        print(f"💵 Before Fees: ${gain_before_fees}")
                        print(f"💵 After  Fees: ${gain_after_fees}")
                        print(f"📊 Amount: {AMOUNTS[currency]} {currency}")
                        print(f"📈 Waiting for BUY signal acheives pay below ${gain_after_fees} gain")
                        print(f"{'='*60}\n")

                    elif SIDE == 'BUY':
                        pay_before_fees = round(float(order.filled_value), 2)
                        pay_after_fees = round(float(order.total_value_after_fees), 2)
                        trading_state['last_buy_pay'] = pay_after_fees
                        trading_state['position'] = 'waiting_sell'
                        
                        print(f"✅ ${SIDE} executed at ${execution_price}")
                        print(f"💸 Fees: ${fees}")
                        print(f"💵 Before Fees: ${pay_before_fees}")
                        print(f"💵 After  Fees: ${pay_after_fees}")
                        print(f"📊 Amount: {AMOUNTS[currency]} {currency}")
                        print(f"📈 Waiting for SELL signal acheives above ${pay_after_fees} pay")
                        print(f"{'='*60}\n")
                    
                    save_state(trading_state, currency)
                    return True
                elif order.status == "OPEN":
                    print(f"⏳ Still waiting... ({i*2}s)")
                else:
                    print(f"❌ Coinbase order failed with status {order.status}")
                    break
            
            print(f"⏰ Timeout after 300s (5m) - cancelling order")
            client.cancel_orders([order_id])
            return False

        else:
            print(f"❌ Coinbase order failed: {order_response}")
            return False
            
    except Exception as e:
        print(f"❌ Error executing ${SIDE}: {str(e)}")
        return False

def execute_buy_order(signal_data):
    """Execute BUY order only if profitable"""
    price = signal_data.get('price')
    product_id = signal_data.get('ticker')
    currency = product_id[:3]
    trading_state = load_state(currency)
    last_sell_gain = trading_state['last_sell_gain']
    
    estimate_pay_before_fees = price * float(AMOUNTS[currency])
    estimate_pay_after_fees = round(estimate_pay_before_fees + (estimate_pay_before_fees * FEES_ESTIMATE), 2) 

    # Check if BUY is profitable
    if estimate_pay_after_fees + MIN_PROFIT >= last_sell_gain:
        print(f"\n⚠️ BUY SKIPPED - No profit opportunity")
        print(f"   Current price: ${price}")
        print(f"   Estimated pay after fees: ${estimate_pay_after_fees:.2f} greater than last SELL gain")
        print(f"   Last SELL gain: ${last_sell_gain}")
        return False
    
    print(f"\n{'='*60}")
    print(f"🟢 EXECUTING BUY ORDER")
    print(f"{'='*60}")
    
    try:
        # Place BUY order on Coinbase
        order_response = client.limit_order_gtc_buy(client_order_id=str(uuid.uuid4()), product_id=product_id,\
                                            base_size=AMOUNTS[currency], limit_price=str(round(price * 1.001, 2)))

        if order_response.success:

            order_id = order_response.success_response["order_id"]

            # NOW wait for it to be FILLED
            for i in range(150):  # 300 seconds
                time.sleep(2) # check every 2 seconds

                get_response = client.get_order(order_id)  # Returns GetOrderResponse
                order = get_response.order  # Order object

                if order.status == "FILLED":

                    pay_after_fees = round(float(order.total_value_after_fees), 2)
                    pay_before_fees = round(float(order.filled_value), 2)
                    execution_price = round(float(order.average_filled_price), 2)
                    fees = round(float(order.total_fees), 2)

                    # Calculate long profit
                    profit = (last_sell_gain - pay_after_fees)
                    trading_state['total_profit'] += profit
                    trading_state['last_buy_pay'] = pay_after_fees
                    trading_state['position'] = 'waiting_sell'
                        
                    print(f"✅ BUY executed at ${execution_price}")
                    print(f"💸 Fees: ${fees}")
                    print(f"💵 Before Fees: ${pay_before_fees}")
                    print(f"💵 After  Fees: ${pay_after_fees}")
                    print(f"📊 Amount: {AMOUNTS[currency]} {currency}")
                    print(f"💵 Trade Profit: ${profit:.2f}")
                    print(f"💰 Total Profit: ${trading_state['total_profit']:.2f}")
                    print(f"📈 Waiting for SELL signal above ${pay_after_fees} pay")
                    print(f"{'='*60}\n")

                    save_state(trading_state, currency)
                    return True
                elif order.status == "OPEN":
                    print(f"⏳ Still waiting... ({i*2}s)")
                else:
                    print(f"❌ Coinbase order failed with status {order.status}")
                    break
           
            print(f"⏰ Timeout after 300s (5m) - cancelling order")
            client.cancel_orders([order_id])
            return False
            
        else:
            print(f"❌ Coinbase order failed: {order_response}")
            return False
            
    except Exception as e:
        print(f"❌ Error executing BUY: {str(e)}")
        return False

def execute_sell_order(signal_data):
    """Execute SELL order only if profitable"""
    price = signal_data.get('price')

    if MINIMUM_ACT_PRICE != 0 and price < MINIMUM_ACT_PRICE:
        print(f"⚠️ Current price ${price} is below minimum activation price ${MINIMUM_ACT_PRICE}. Skipping SELL order.")
        return False

    product_id = signal_data.get('ticker')
    currency = product_id[:3]
    trading_state = load_state(currency)
    last_buy_pay = trading_state['last_buy_pay']
    
    estimate_gain_before_fess = float(price) * float(AMOUNTS[currency])
    estimate_gain_after_fees = round(estimate_gain_before_fess - (estimate_gain_before_fess * FEES_ESTIMATE), 2)

    # Check if SELL is profitable
    if CHECK_PREV_BUY and estimate_gain_after_fees <= last_buy_pay:
        print(f"\n⚠️ SELL SKIPPED - No profit opportunity")
        print(f"   Current price: ${price}")
        print(f"   Estimated gain: ${estimate_gain_after_fees} less than last BUY pay")
        print(f"   Last BUY pay: ${last_buy_pay}")
        return False
    
    print(f"\n{'='*60}")
    print(f"🔴 EXECUTING SELL ORDER")
    print(f"{'='*60}")
    
    try:
        # Place SELL order on Coinbase
        order_response = client.limit_order_gtc_sell(client_order_id=str(uuid.uuid4()), product_id=product_id,\
                                            base_size=AMOUNTS[currency], limit_price=str(round(price * 0.999, 2)))

        if order_response.success:

            order_id = order_response.success_response["order_id"]

            # NOW wait for it to be FILLED
            for i in range(150):  # 300 seconds
                time.sleep(2) # check every 2 seconds
                    
                # get executed price
                get_response = client.get_order(order_id)  # Returns GetOrderResponse
                order = get_response.order  # Order object

                if order.status == "FILLED":

                    print("✅ Order FILLED!")
                    gain_before_fees = round(float(order.filled_value), 2)
                    gain_after_fees = round(float(order.total_value_after_fees), 2)
                    execution_price = round(float(order.average_filled_price), 2)
                    fees = round(float(order.total_fees), 2)

                    # Calculate long profit
                    profit = (gain_after_fees - last_buy_pay)
                    trading_state['total_profit'] += profit
                    trading_state['last_sell_gain'] = gain_after_fees
                    trading_state['position'] = 'waiting_buy'
                        
                    print(f"✅ SELL executed at ${execution_price}")
                    print(f"💸 Fees: ${fees}")
                    print(f"💵 Before Fees: ${gain_before_fees}")
                    print(f"💵 After  Fees: ${gain_after_fees}")
                    print(f"📊 Amount: {AMOUNTS[currency]} {currency}")
                    print(f"💵 Trade Profit: ${profit:.2f}")
                    print(f"💰 Total Profit: ${trading_state['total_profit']:.2f}")
                    print(f"📈 Waiting for BUY signal below ${gain_after_fees} gain")
                    print(f"{'='*60}\n")

                    save_state(trading_state, currency)
                    return True
                elif order.status == "OPEN":
                    print(f"⏳ Still waiting... ({i*2}s)")
                else:
                    print(f"❌ Coinbase order failed with status {order.status}")
                    break

            print(f"⏰ Timeout after 300s (5m) - cancelling order")
            client.cancel_orders([order_id])
            return False
            
        else:
            print(f"❌ Coinbase order failed: {order_response}")
            return False
            
    except Exception as e:
        print(f"❌ Error executing SELL: {str(e)}")
        return False

def verify_tradingview_token(data):
    return data.get('token') == SECRET_TOKEN

# Flask Routes
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if request.content_type and 'application/json' not in request.content_type:
            # Try parsing as text
            data = json.loads(request.data.decode('utf-8'))
        else:
            data = request.get_json(force=True)
        
        if not data:
            abort(400, description="No JSON data")
        
        # Verify token
        if not verify_tradingview_token(data):
            print(f"❌ Invalid token")
            abort(403, description="Invalid token")
        
        action = data.get('action', '')
        price = data.get('price')
        currency = data.get('ticker', '')[:3]  # Extract currency from ticker (e.g. BTC from BTCGBP)

        # Log incoming signal
        print(f"\n📨 Signal received: {action} at ${price}")
        
        trading_state = load_state(currency)

        # Trading Strategy Logic
        if trading_state['position'] is None:
            # Initial state - wait for first SELL
            if action == SIDE:
                execute_first_order(data)
            else:
                print(f"⏳ Waiting for first ${SIDE} signal...")
                
        elif trading_state['position'] == 'waiting_buy':
            # After a SELL, waiting for profitable BUY
            if action == 'BUY':
                execute_buy_order(data)
            else:
                print("⏭️ Ignoring SELL - waiting for BUY signal")
                
        elif trading_state['position'] == 'waiting_sell':
            # After a BUY, waiting for profitable SELL
            if action == 'SELL':
                execute_sell_order(data)
            else:
                print("⏭️ Ignoring BUY - waiting for SELL signal")
        
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    PORT = int(os.environ.get('PORT'))
    
    # Load amounts configuration
    amounts_file = "/home/ubuntu/workspace/crypto_trading_bot/amounts.json"
    if os.path.exists(amounts_file):
        with open(amounts_file, "r") as f:
            AMOUNTS = json.load(f)
            print(f"✅ Loaded amounts from {amounts_file}")
    else:
        print(f"⚠️ amounts.json not found at {amounts_file}")
        return 
    
    if len(sys.argv) > 1 and sys.argv[1] in ['BUY', 'SELL']:
        SIDE = sys.argv[1]
    if len(sys.argv) > 2:
        MINIMUM_ACT_PRICE = int(sys.argv[2])

    print(f"\n{'='*60}")
    print(f"🤖 COINBASE TRADING BOT STARTING")
    print(f"{'='*60}")
    print(f"🔒 Server port: {PORT}")
    print(f"📡 Webhook: http://0.0.0.0:{PORT}/webhook")
    print(f"💱 Trading pair: XXX-GBP")
    print(f"📋 Amounts : {AMOUNTS}")
    print(f"Act Minimum: {MINIMUM_ACT_PRICE}")
    print(f"⏳ Strategy: Waiting for first ${SIDE} signal...")
    print(f"{'='*60}\n")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)