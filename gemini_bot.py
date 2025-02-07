#!/usr/bin/env python

import argparse
import configparser
import datetime
import decimal
import json
import math
import requests
import os
import time

from decimal import Decimal

from gemini_api import GeminiApiConnection, GeminiRequestException

"""
    Gemini API docs: https://docs.gemini.com/rest-api/
"""


def get_timestamp():
    ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


parser = argparse.ArgumentParser(
    description="""
        Basic Gemini DCA buying/selling bot.

        ex:
            BTCUSD BUY 14 USD          (buy $14 worth of BTC)
            BTCUSD BUY 0.00125 BTC     (buy 0.00125 BTC)
            ETHBTC SELL 0.00125 BTC    (sell 0.00125 BTC worth of ETH)
            ETHBTC SELL 0.1 ETH        (sell 0.1 ETH)
    """,
    formatter_class=argparse.RawTextHelpFormatter
)

# Required positional arguments
parser.add_argument('market_name', help="(e.g. BTCUSD, ETHBTC, etc)")

parser.add_argument('order_side',
                    type=str,
                    choices=["BUY", "SELL"])

parser.add_argument('amount',
                    type=Decimal,
                    help="The quantity to buy or sell in the amount_currency")

parser.add_argument('amount_currency',
                    help="The currency the amount is denominated in")

# Additional options
parser.add_argument('-sandbox',
                    action="store_true",
                    default=False,
                    dest="sandbox_mode",
                    help="Run against sandbox, skips user confirmation prompt")

parser.add_argument('-warn_after',
                    default=300,
                    action="store",
                    type=int,
                    dest="warn_after",
                    help="secs to wait before sending an alert that an order isn't done")

parser.add_argument('-j', '--job',
                    action="store_true",
                    default=False,
                    dest="job_mode",
                    help="Suppresses user confirmation prompt")

parser.add_argument('-c', '--config',
                    default="settings.conf",
                    dest="config_file",
                    help="Override default config file location")


if __name__ == "__main__":
    args = parser.parse_args()

    market_name = args.market_name
    order_side = args.order_side.lower()
    amount = args.amount
    amount_currency = args.amount_currency

    sandbox_mode = args.sandbox_mode
    job_mode = args.job_mode
    warn_after = args.warn_after

    if not sandbox_mode and not job_mode:
        response = input("Production purchase! Confirm [Y]: ")
        if response != 'Y':
            print("Exiting without submitting purchase.")
            exit()

    # Read settings
    config = configparser.ConfigParser()
    config.read(args.config_file)

    config_section = 'production'
    if sandbox_mode:
        config_section = 'sandbox'

    client_key = config.get(config_section, 'CLIENT_KEY')
    secret_key = config.get(config_section, 'CLIENT_SECRET')

    gemini_api_conn = GeminiApiConnection(client_key=client_key, client_secret=secret_key)

    # Configure the market details
    symbol_details = gemini_api_conn.symbol_details(market_name)

    base_currency = symbol_details.get("base_currency")
    quote_currency = symbol_details.get("quote_currency")
    base_min_size = Decimal(str(symbol_details.get("min_order_size"))).normalize()
    base_increment = Decimal(str(symbol_details.get("tick_size"))).normalize()
    quote_increment = Decimal(str(symbol_details.get("quote_increment"))).normalize()
    if amount_currency == symbol_details.get("quote_currency"):
        amount_currency_is_quote_currency = True
    elif amount_currency == symbol_details.get("base_currency"):
        amount_currency_is_quote_currency = False
    else:
        raise Exception(f"amount_currency {amount_currency} not in market {market_name}")

    print(f"base_min_size: {base_min_size}")
    print(f"base_increment: {base_increment}")
    print(f"quote_increment: {quote_increment}")

    
    def calculate_midmarket_price():
        order_book = gemini_api_conn.current_order_book(market_name)

        bid = Decimal(order_book.get('bids')[0].get('price')).quantize(quote_increment)
        ask = Decimal(order_book.get('asks')[0].get('price')).quantize(quote_increment)

        # Avg the bid/ask but round to nearest quote_increment
        if order_side == "buy":
            midmarket_price = (math.floor((ask + bid) / Decimal('2.0') / quote_increment) * quote_increment).quantize(quote_increment, decimal.ROUND_DOWN)
        else:
            midmarket_price = (math.floor((ask + bid) / Decimal('2.0') / quote_increment) * quote_increment).quantize(quote_increment, decimal.ROUND_UP)
        print(f"ask: ${ask}")
        print(f"bid: ${bid}")
        print(f"midmarket_price: ${midmarket_price}")

        return midmarket_price


    def place_order(price):
        try:
            if amount_currency_is_quote_currency:
                result = gemini_api_conn.new_order(
                    market=market_name,
                    side=order_side,
                    amount=float((amount / price).quantize(base_increment)),
                    price=price
                )
            else:
                result = gemini_api_conn.new_order(
                    market=market_name,
                    side=order_side,
                    amount=float(amount.quantize(base_increment)),
                    price=price
                )
        except GeminiRequestException as e:
            print(f"ERROR placing {base_currency} {order_side} order: {e.response_json.get('reason')}")
            print(json.dumps(e.response_json, indent=4))
            exit()
        return result


    midmarket_price = calculate_midmarket_price()
    order = place_order(midmarket_price)

    print(json.dumps(order, indent=2))

    order_id = order.get("order_id")

    # Set up monitoring loop for the next hour
    wait_time = 60
    total_wait_time = 0
    retries = 0
    while Decimal(order.get('remaining_amount')) > Decimal('0'):
        if total_wait_time > warn_after:
            print(f"{market_name} {order_side} order of {amount} {amount_currency} OPEN/UNFILLED")
            exit()

        if order.get('is_cancelled'):
            # Most likely the order was manually cancelled in the UI
            print(f"{market_name} {order_side} order of {amount} {amount_currency} CANCELLED")
            exit()

        print(f"{get_timestamp()}: Order {order_id} still pending. Sleeping for {wait_time} (total {total_wait_time})")
        time.sleep(wait_time)
        total_wait_time += wait_time
        order = gemini_api_conn.order_status(order_id=order_id)

    # Order status is no longer pending!
    print(json.dumps(order, indent=2))

    subject = f"{market_name} {order_side} order of {amount} {amount_currency} complete @ {midmarket_price} {quote_currency}"
    print(subject)

