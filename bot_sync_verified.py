from flask import Flask, request, jsonify
from ib_insync import *
import asyncio
import requests
import traceback
from datetime import datetime
import pytz
import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
import csv
import os

ib = IB()

# Create a new event loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

print("🚀 Running FULLY SYNC CLEAN VERSION")

# Load environment (automatically from Railway or local .env)
load_dotenv()

SECRET_TOKEN = os.getenv("SECRET_TOKEN")
print("✅ Loaded SECRET_TOKEN:", SECRET_TOKEN)

MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "my_secure_token_123")
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", 1000))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 1)) / 100
TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "executed_trades.csv")
TEST_MODE = False

app = Flask(__name__)

ib = IB()

open_orders = {}

def catch_all_requests():
    print("📩 Incoming Request")
    print("Method:", request.method)
    print("Path:", request.path)
    try:
        print("Payload:", request.get_json(force=True))
    except Exception as e:
        print("⚠️ Error parsing JSON:", str(e))

def send_email(subject, text):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg.set_content(text)

        response = requests.post(
            f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            auth=("api", MAILGUN_API_KEY),
            data={"from": EMAIL_SENDER, "to": EMAIL_RECEIVER, "subject": subject, "text": text}
        )
        print("📧 Email sent:", response.status_code)
    except Exception as e:
        print("❌ Failed to send email:", str(e))

def calculate_qty(entry, stop, risk_pct, account_size):
    risk_amount = account_size * risk_pct
    stop_diff = abs(entry - stop)
    return max(1, int(risk_amount / stop_diff)) if stop_diff else 1

def log_trade(symbol, entry, qty, stop_loss, take_profit, side, reason="entry", status="open", exit_price="", pnl=""):
    now = datetime.now()
    trade_data = [
        now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), symbol, side, qty,
        round(entry, 2), round(stop_loss, 2), round(take_profit, 2), reason,
        status, exit_price, pnl
    ]
    header = [
        "Date", "Time", "Symbol", "Side", "Quantity", "Entry Price", "Stop Loss", "Take Profit",
        "Reason", "Status", "Exit Price", "PnL"
    ]
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(header)
        writer.writerow(trade_data)

def handle_order_status(order):
    if order.orderId in open_orders:
        symbol, qty, entry, stop, tp, side = open_orders[order.orderId]
        if order.status in ["Filled", "Cancelled"]:
            exit_price = order.lmtPrice if order.lmtPrice else ""
            status = order.status.lower()
            pnl = (float(exit_price) - entry) * qty if side == "BUY" else (entry - float(exit_price)) * qty
            log_trade(symbol, entry, qty, stop, tp, side, reason="exit", status=status, exit_price=exit_price, pnl=round(pnl, 2))
            del open_orders[order.orderId]

ib.orderStatusEvent += handle_order_status

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        data = request.get_json(force=True)
        token = data.get("token")

        print("🔔 Incoming Webhook")
        print("Token received:", token)
        print("Expected token:", SECRET_TOKEN)

        if token != SECRET_TOKEN:
            print("❌ Invalid token — rejecting request")
            return jsonify({"error": "unauthorized"}), 403

        symbol = data.get("symbol")
        side = data.get("side").upper()
        entry = float(data.get("entry"))
        stop = float(data.get("stop"))
        qty = calculate_qty(entry, stop, RISK_PERCENT, ACCOUNT_SIZE)
        tp = entry + (entry - stop) * 2 if side == "BUY" else entry - (stop - entry) * 2

        print(f"✅ Processing {side} order for {symbol} | Qty: {qty} | Entry: {entry} | Stop: {stop} | TP: {tp}")

        try:
            ib.connect("127.0.0.1", 4002, clientId=22)
        except Exception as connect_err:
            print(f"❌ Failed to connect to IBKR: {connect_err}")
            return jsonify({"error": "Failed to connect to IBKR"}), 500

        if not ib.isConnected():
            print("❌ IBKR not connected after attempt")
            return jsonify({"error": "IBKR not connected"}), 500

        try:
            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)
        except Exception as qerr:
            print(f"❌ Failed to qualify contract: {qerr}")
            return jsonify({"error": "Contract qualification failed"}), 500

        try:
            bracket = ib.bracketOrder(
                action=side,
                quantity=qty,
                limitPrice=entry,
                takeProfitPrice=tp,
                stopLossPrice=stop
            )

            bracket[0].transmit = False
            bracket[1].transmit = True
            bracket[2].transmit = True

            for order in bracket:
                trade = ib.placeOrder(contract, order)
                print(f"📤 Sent order ID {order.orderId} | Status: {trade.orderStatus.status}")
                if trade.orderStatus.status != "Submitted":
                    print("⚠️ Order was not submitted successfully:", trade.orderStatus.status)
                open_orders[order.orderId] = (symbol, qty, entry, stop, tp, side)

            log_trade(symbol, entry, qty, stop, tp, side)
            print("✅ Trade successfully logged and orders placed.")
            return jsonify({"status": "success", "message": "Order sent"}), 200

        except Exception as place_err:
            print("❌ Error placing order:", str(place_err))
            return jsonify({"error": f"Order failed: {str(place_err)}"}), 500

    except Exception as e:
        print("❌ Webhook error:", str(e))
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        ib.disconnect()
        print("🔌 Disconnected from IBKR")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
