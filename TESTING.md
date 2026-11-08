# Testing

Install test dependencies and run the local mapping tests:

```sh
python -m pip install pytest
python -m pytest -q
```

For manual webhook testing, configure `WIZARR_API_KEY`, `WIZARR_WEBHOOK_PATH`,
and `WIZARR_WEBHOOK_PORT`, then send a test request to the configured endpoint.
Use non-production test accounts and never include real invite codes or API
credentials in issue reports.
