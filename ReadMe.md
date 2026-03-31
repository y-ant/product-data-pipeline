product-data-pipeline
An ETL pipeline designed to pull, process, and store product data; all of it automated.

### Overview

This project automates price monitoring for a specific list of competitor products to help shop managers compare their pricing strategy.

Target URLs and sensitive identifiers are stored in GitHub Secrets to avoid exposing scraping targets.

Built using Playwright with an asynchronous architecture for fast, reliable data extraction.

### Tech Stack

Language: Python (Asyncio)

Browser Automation: Playwright

Orchestration: GitHub Actions

### Automation

The pipeline is fully managed via GitHub Actions:

Scheduled: Runs daily to ensure price parity.

Manual: Can be dispatched via the Actions tab for ad-hoc runs.

Secure: All environment variables are injected at runtime via encrypted secrets.

### Local Setup

Clone the repo.

Install dependencies: pip install -r requirements.txt

Install Playwright: playwright install chromium

Configure: Populate your local .env file with the required secrets.

### License

MIT