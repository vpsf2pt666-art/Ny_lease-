# NY Lease Review

A web app for New York tenants. They paste or upload their lease, and an AI (through OpenRouter) flags clauses that are unfair, unusual or likely unenforceable under NY law. It also lists missing disclosures and suggests questions to ask the landlord.

## Setup

1. Get an API key at https://openrouter.ai/keys and add credit to your account.
2. Copy `.env.example` to `.env` and paste your key in as `OPENROUTER_API_KEY`.
3. Run the server (Python 3; nothing to install):

   ```
   python3 server.py
   ```

4. Open http://localhost:8000

## How the key stays safe

Browsers only talk to `server.py`. The server adds your key and calls OpenRouter, so the key never reaches users' browsers. Two protections stop strangers from using up your credit:

- a per-visitor hourly limit (`MAX_REVIEWS_PER_HOUR`)
- an optional shared password (`APP_PASSWORD`)

It's also worth setting a spending limit on the key in your OpenRouter dashboard.

## Files

- `server.py`: the backend (key handling, rate limiting, OpenRouter call)
- `prompt.md`: the AI's instructions and the NY tenant-law checklist. Edit this to change how leases are reviewed.
- `public/index.html`: the page tenants use

## Deploying to Render

`render.yaml` configures everything. In Render, choose **New → Blueprint**, pick this repository and paste your OpenRouter key when asked. The key is stored in Render's environment settings, never in the code.

Limits you can change under the service's **Environment** tab:
- `MAX_REVIEWS_PER_HOUR`: per visitor (default 10)
- `MAX_REVIEWS_PER_DAY`: whole site (default 100, roughly $10–15/day at most)
- `APP_PASSWORD`: set this to make the site private

On Render's free plan, the app goes to sleep after 15 minutes without visitors. The next visit then takes about a minute to load.

## Before going public

- Have a NY tenant attorney review `prompt.md` and some sample outputs. The law changes, and the AI can make mistakes.
- Set a spending limit on your OpenRouter key.
- Review OpenRouter's and the chosen model provider's data policies, since users' leases contain personal information.
