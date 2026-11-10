#!/usr/bin/env bash
# Give the local daemon a public HTTPS address so PayPal can deliver webhooks.
#
#   1. Install ngrok (https://ngrok.com/download) and run:  ngrok config add-authtoken <your token>
#   2. Start the daemon:   scripts/run_dev.sh
#   3. Run this script.    It prints a URL like https://abc123.ngrok-free.app
#   4. In developer.paypal.com > Apps & Credentials > your app > Webhooks, add
#        <that URL>/webhooks/paypal
#      and subscribe to: Customer dispute created / updated / resolved,
#      Invoicing invoice paid, Billing subscription payment failed.
#   5. Copy the Webhook ID PayPal shows into .env as PAYPAL_WEBHOOK_ID, then restart the daemon.
set -euo pipefail
PORT="${AGENTOS_PORT:-8000}"
command -v ngrok >/dev/null || { echo "ngrok is not installed: https://ngrok.com/download"; exit 1; }
echo "Webhook URL will be:  <the https URL below>/webhooks/paypal"
exec ngrok http "$PORT"
