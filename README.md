# PayoutPilot

**AI judge → human approval → PayPal Payouts**, for bounties, creator campaigns, ambassador programs and small hackathons.

Sponsors collect hundreds of submissions and then spend days reviewing them and paying winners one by one.
PayoutPilot does the boring, error-prone parts and keeps a human in charge of the money:

1. **Integrity checks (deterministic):** missing fields, invalid PayPal emails, the same payee entered twice,
   the same link submitted twice, copy-paste near-duplicates, and wallet / seed-phrase bait.
2. **AI judge with reasons:** each clean submission is scored against the campaign rubric by any
   OpenAI-compatible model (Gemini, Groq, local Ollama), then a **reviewer pass** checks the scores against the content.
   Prompt-injection inside a submission is ignored by design (content is fenced, the model only returns JSON).
3. **Prize allocator:** tiers, multi-place tiers, minimum score, budget cap, stable tie-breaks.
4. **Approval gate (AG Grid dashboard):** exclude anyone, re-plan, then approve by typing the exact total.
5. **PayPal Payouts:** one batch per approved plan with a deterministic `sender_batch_id` + `PayPal-Request-Id`,
   so a double click or retry can never double-pay. Per-item status (SUCCESS / UNCLAIMED / …) shown in the grid.

## Quick start (offline, no keys)

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export PAYPAL_MODE=mock LLM_PROVIDER=mock
python -m payoutpilot run examples/campaign.json examples/submissions.csv --pay --yes
python -m payoutpilot serve      # http://localhost:8000
python -m pytest -q
```

## Real run (PayPal sandbox + a real model)

```bash
cp .env.example .env   # fill PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox app) and GEMINI_API_KEY
set -a; . ./.env; set +a
python -m payoutpilot run examples/campaign.json examples/submissions.csv --pay
```

Use **sandbox personal accounts** (developer.paypal.com → Testing tools → Sandbox accounts) as the payee emails to see
payouts land. Live mode is locked unless `PAYOUTPILOT_ALLOW_LIVE=1` is set on purpose.

## API

| Method | Path | What |
| --- | --- | --- |
| POST | `/api/campaigns` | create campaign (brief, rubric, prizes, budget) |
| POST | `/api/campaigns/{id}/submissions` or `/submissions.csv` | load submissions, run integrity checks |
| POST | `/api/campaigns/{id}/judge` | AI judge + reviewer + payout plan |
| POST | `/api/campaigns/{id}/exclude` | manual exclusions, re-plan |
| POST | `/api/campaigns/{id}/approve` | `{"confirm_total": "310.00"}` → PayPal batch payout |
| POST | `/api/campaigns/{id}/refresh` | pull payout batch + item status from PayPal |

## Architecture

```
CSV / JSON ─► integrity.py ─► judge.py (LLM judge + reviewer) ─► allocator.py ─► AG Grid approval ─► paypal.py
                                                                                                  OAuth2 → /v1/payments/payouts
state: data/<campaign>.json (atomic writes)                                                      → GET batch status
```

## Safety

Sandbox by default, live locked. No auto-pay: every batch needs an explicit approval with the exact total and a budget
check. Secrets only come from the environment and are never logged.

## License

MIT
