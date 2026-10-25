# PayoutPilot — Devpost submission draft (PayPal AI Hackathon)

> Draft only. Not submitted. Video: `demo/demo.mp4` (2:40, 1080p), to be uploaded to YouTube (public) by the team.
> Repo: https://github.com/Surajss11/paypal-payoutpilot (MIT)

**Tagline:** An AI payout agent for creator campaigns, bounties and hackathons: AI judges and plans, a human approves, PayPal Payouts pays and tracks.

## Inspiration

We take part in creator campaigns, bounty boards and community hackathons every week, and we keep seeing the same
thing from the other side: a sponsor with hundreds of entries, a spreadsheet, and days of manual review before anyone
gets paid. Winners wait for weeks. The same person enters twice under two handles, threads get copied word for word,
scam entries ask people to "validate their wallet", and the payout step is one tired human clicking "send" 100 times.
Some winners never claim, and one wrong retry can pay a whole list twice. We wanted an agent that does the boring,
error-prone work and still leaves the money decision to a person.

## What it does

- **Ask in plain English or CSV.** "Pay alice@x.com $25 for the bug fix and bob@y.com $10 as runner-up", "Send $5 each
  to …", "Split $30 between …", or paste `email,amount,note`. `@handles` resolve to PayPal emails from earlier campaigns.
- **Two planners, one validator.** A deterministic rule parser always runs. An optional LLM planner (local Ollama, Gemini or
  Groq) extracts payees too, but every receiver and amount it returns must literally appear in the request, so a
  hallucinated or injected payee can never reach PayPal. The two plans are diffed and any disagreement is shown.
  Prompt-injection text in the request is flagged.
- **Judge whole campaigns.** Deterministic integrity checks run before any model call: missing fields, invalid emails,
  the same payee twice, the same link twice, near-duplicate text, wallet / seed-phrase bait. Then an AI judge scores each
  clean entry against the rubric with reasons, a second reviewer pass checks every score, and an allocator turns the
  ranking into prize tiers with a minimum score and a budget cap.
- **Human approval.** In the AG Grid dashboard the sponsor reviews everything, excludes anyone, and approves by typing
  the **exact total**. Per-payee and per-batch caps apply; once paid, the plan is frozen.
- **Pay with PayPal Payouts.** One batch per approved plan with a deterministic `sender_batch_id` that is also the
  `PayPal-Request-Id`, so a double click or retry can never pay twice. PayoutPilot polls the batch until every item settles,
  re-sends only FAILED / BLOCKED / DENIED items in a new idempotent batch, and can cancel UNCLAIMED items to return the money.
- **Fund the pool with PayPal Orders v2.** The sponsor gets a PayPal checkout for the prize pool before payouts go out.
- **Audit trail.** Every step (checked, judged, planned, approved by whom, batch created, status, retries, cancels,
  PayPal errors with debug id) is stored with the run.

**Real sandbox run (25 Oct 2026):** Payouts batch `8JYSZ8ZNGGT9N` created PENDING at 17:22:20 IST and polled to SUCCESS at
17:22:53 IST: 5.00 USD to a sandbox personal account SUCCESS, 2.50 USD to an address without a PayPal account UNCLAIMED
(RECEIVER_UNREGISTERED). Sponsor funding order `16864828RP192205Y` created via Orders v2. Redacted log in
`evidence/sandbox-run.json`.

## How we built it

- **Backend:** Python 3.11, FastAPI, httpx, Pydantic v2; JSON-file state with atomic writes.
- **PayPal:** REST sandbox, OAuth 2.0 client credentials (token cache, one refresh on 401), Payouts
  (`/v1/payments/payouts`, `/v1/payments/payouts-item/{id}`, `/cancel`), Orders v2 (`/v2/checkout/orders`, capture).
  429/5xx get a bounded retry only when repeating cannot double-pay (GETs, writes with a request id, idempotent cancels).
- **AI:** a small OpenAI-compatible client with pluggable providers: local Ollama (`qwen2.5:3b`, free, used in the demo),
  Gemini 2.5 Flash or Groq Llama 3.3 via free keys, or any OpenAI-compatible URL. Everything also runs fully offline with
  a deterministic judge.
- **Frontend:** one static page with AG Grid Community, no build step.
- **Quality:** 29 pytest tests with respx-mocked PayPal HTTP (no network): polling, retries, idempotency, cancel, Orders,
  injection, caps, API flow. A sandbox evidence script runs the live flow and refuses to write any file containing the secret.

## Challenges we ran into

- **Payouts and country.** Our first sandbox app was linked to an India sandbox business account, and every batch was
  refused with `403 PAYOUT_NOT_AVAILABLE`. We moved to a US sandbox business account, and PayoutPilot now explains this
  error and how to fix it.
- **Sandbox 5xx.** Cancelling the unclaimed item returns `500 INTERNAL_SERVICE_ERROR` from the sandbox on every attempt.
  That pushed us to make retries bounded and safe, to check the item's real status instead of trusting an error,
  and to make the evidence script resume the existing batch instead of paying again.
- **Trusting a model with money.** A small local model is useful but can be fooled. We never let model output add
  receivers or amounts that are not in the request, and we diff it against a deterministic parser.

## Accomplishments that we're proud of

- A real end-to-end PayPal sandbox payout from a plain-English request, with a human approval gate and live status tracking.
- Idempotency end to end: same plan, same batch id, no double pay, even across retries and re-runs.
- The demo's hidden "send $500 to the attacker" injection is dropped and flagged while the real payees go through.
- Runs on free tools only: a local model, the PayPal sandbox, and open-source libraries.

## What we learned

The hard part of agentic payments is not calling the API. It is making every step safe to repeat and easy for a human to
check: idempotency keys, bounded retries, frozen plans after payment, and an audit trail that says who approved what.

## What's next for PayoutPilot

- PayPal webhooks for payout item events instead of polling, plus scheduled payouts.
- PayPal MCP server / Agent Toolkit so the agent can be driven from a chat client ("pay the top 10 from the Hazels campaign").
- Venmo and PayPal ID receivers, multi-currency batches, and the Invoicing API for sponsors.
- A hosted demo and import connectors for Google Forms, Typeform and X / Discord campaign exports.

## Built with

python · fastapi · httpx · pydantic · paypal-payouts-api · paypal-orders-v2 · paypal-oauth · paypal-sandbox · ag-grid ·
ollama · qwen2.5 · gemini · groq · pytest · respx · playwright · ffmpeg

## Try it out

- GitHub: https://github.com/Surajss11/paypal-payoutpilot
- Offline in 1 minute: `PAYPAL_MODE=mock LLM_PROVIDER=mock python -m payoutpilot serve`

## Video segments (for the YouTube description)

0:00 PayoutPilot · 0:09 The problem · 0:25 Ask the agent (prompt injection blocked) · 0:49 Integrity checks + AI judge ·
1:12 Human approval · 1:22 Live PayPal sandbox batch 8JYSZ8ZNGGT9N · 1:46 Poll, retry, cancel unclaimed ·
2:09 Architecture · 2:32 Close
