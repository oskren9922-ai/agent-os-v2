#!/usr/bin/env python3
"""Download PayPal's official OpenAPI specs and pin them.

    python scripts/download_specs.py            download the pinned commit, verify the hashes
    python scripts/download_specs.py --check    verify the files on disk, no network
    python scripts/download_specs.py --commit <sha> --repin
                                                move to a newer commit and rewrite PINNED.json

The specs come from https://github.com/paypal/paypal-rest-api-specifications
(Apache-2.0). They are committed to this repo and never fetched at runtime:
the compiler must produce the same functions on every machine. After repinning,
run `agentos paypal register` and the tests, because the functions may change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

SPECS_DIR = Path(__file__).resolve().parent.parent / "paypal_adapter" / "specs"
PIN_FILE = SPECS_DIR / "PINNED.json"
RAW = "https://raw.githubusercontent.com/paypal/paypal-rest-api-specifications/{commit}/openapi/{name}"

# local file name -> file name in PayPal's repository
FILES = {
    "customer_disputes_v1.json": "customer_disputes_v1.json",
    "invoicing_v2.json": "invoicing_v2.json",
    "checkout_orders_v2.json": "checkout_orders_v2.json",
    "payments_v2.json": "payments_payment_v2.json",
    "billing_subscriptions_v1.json": "billing_subscriptions_v1.json",
    "reporting_transactions_v1.json": "reporting_transactions_v1.json",
    "notifications_webhooks_v1.json": "notifications_webhooks_v1.json",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pin() -> dict:
    return json.loads(PIN_FILE.read_text(encoding="utf-8"))


def check() -> int:
    pin = load_pin()
    bad = 0
    for local, expected in pin["sha256"].items():
        path = SPECS_DIR / local
        actual = sha256(path.read_bytes()) if path.is_file() else "missing"
        ok = actual == expected
        bad += not ok
        print(f"{'ok      ' if ok else 'MISMATCH'} {local}")
    print(f"\nPinned to commit {pin['commit']}")
    return 1 if bad else 0


def download(commit: str, repin: bool) -> int:
    pin = load_pin() if PIN_FILE.is_file() else {"sha256": {}}
    hashes: dict[str, str] = {}
    for local, remote in FILES.items():
        url = RAW.format(commit=commit, name=remote)
        print(f"downloading {remote} ...")
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
            data = response.read()
        json.loads(data)  # must be valid JSON
        digest = sha256(data)
        expected = pin["sha256"].get(local)
        if not repin and expected and digest != expected:
            print(f"  hash mismatch for {local}: expected {expected}, got {digest}", file=sys.stderr)
            return 1
        (SPECS_DIR / local).write_bytes(data)
        hashes[local] = digest
    if repin or not PIN_FILE.is_file():
        PIN_FILE.write_text(json.dumps({
            "source": "https://github.com/paypal/paypal-rest-api-specifications",
            "license": "Apache-2.0",
            "commit": commit,
            "sha256": hashes,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"pinned {len(hashes)} specs to {commit}")
    else:
        print(f"{len(hashes)} specs match the pin ({commit})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="verify files on disk against PINNED.json")
    parser.add_argument("--commit", help="commit of PayPal's spec repository (default: the pinned one)")
    parser.add_argument("--repin", action="store_true", help="accept new hashes and rewrite PINNED.json")
    args = parser.parse_args()
    if args.check:
        return check()
    commit = args.commit or load_pin()["commit"]
    return download(commit, args.repin)


if __name__ == "__main__":
    raise SystemExit(main())
