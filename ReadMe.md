# Product Data Pipeline

An automated ETL pipeline for extracting, processing, and monitoring competitor product prices.

---

## Overview

This project enables automated price monitoring for a selected set of products, helping shop manager evaluate and adjust their pricing strategies.

- Extracts product data from target websites using browser automation  
- Processes and structures pricing data  
- Stores results for analysis and comparison  

Sensitive targets and identifiers are managed securely via environment variables and GitHub Secrets.

---

## Tech Stack

- **Language:** Python (asyncio)  
- **Browser Automation:** Playwright  
- **Orchestration:** GitHub Actions  

---

## Architecture

The pipeline follows a simple ETL flow:

1. **Extract**  
   - Scrape product data using Playwright (asynchronous)

2. **Transform**  
   - Clean and normalize price and product data  

3. **Load**  
   - Store structured output for later analysis  

---

## Automation

The pipeline is fully automated using GitHub Actions:

- **Scheduled runs:** Executes daily to track price changes  
- **Manual runs:** Triggered on demand via the Actions tab  
- **Secure configuration:** Environment variables managed via encrypted secrets  

---

## Local Setup

### 1. Create and activate virtual environment

```bash
uv venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
uv pip install -r requirements.txt
```

### 3. Install Playwright browsers

```bash
playwright install chromium
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
BASE_URL=your_target_url
```

### 5. Run the pipeline

```bash
python run_cli.py
```

```
