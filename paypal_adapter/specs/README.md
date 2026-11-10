# PayPal OpenAPI specs (pinned)

These files are PayPal's official REST API descriptions, copied unchanged from
<https://github.com/paypal/paypal-rest-api-specifications> (Apache-2.0).
`payments_v2.json` is PayPal's `payments_payment_v2.json`, renamed.

`PINNED.json` records the source commit and a SHA-256 for each file.

    python scripts/download_specs.py --check    # verify the files, no network
    python scripts/download_specs.py            # re-download the pinned commit

The compiler reads these at connect time only. Nothing is fetched at runtime.
