# PayoutPilot — PayPal AI Hackathon build plan

Hackathon: PayPal AI Hackathon (Devpost, managed by Devpost) — https://paypalaihackathon.devpost.com/
Deadline: Nov 12, 2026 12:00 PM PST = **Nov 13, 2026 1:30 AM IST**. Judging Dec 1–15, winners ~Dec 21.
Prizes: $67,500 cash. 1st $12,000, 2nd $8,000, 3rd $5,000; $5,000 each for Most Creative, Most Impactful,
Best Demo Delivery, Best Use of PayPal + AI, Best Use of Agentic Commerce; AG Grid $5k/$2k/$1k×3; APIMatic $1k×3;
Bryntum $1k×3; Channel3 $1.5k.
Eligibility: India allowed (only Brazil, Quebec, Russia, Crimea, Cuba, Iran, North Korea, OFAC-sanctioned excluded).
Rules: PayPal developer platform (free **sandbox**) + any AI tool. Public repo with OSI license visible in About,
working build (setup instructions or hosted URL), text description, **video < 3 min on YouTube (public)**.
Judging (equal weight): Technological implementation, Design, Potential impact, Innovation, Presentation.

## Idea

**PayoutPilot: an AI judge-and-pay agent for bounties, creator campaigns and hackathons.**
Campaign sponsors (Web2 and Web3 creator programs, bounty boards, ambassador programs, small hackathons) collect
hundreds of submissions, then spend days reviewing them and paying winners by hand. PayoutPilot:

1. Imports a campaign brief + rubric + prize table, and submissions (CSV / form export / JSON).
2. **Deterministic integrity checks** first: duplicate payout emails, duplicate links, near-duplicate text,
   unreachable links, missing required fields, wallet/seed-phrase bait in the content.
3. **AI judge** scores each submission against the rubric with cited reasons (any OpenAI-compatible model:
   Gemini, Groq, local Ollama). A second reviewer pass checks the score against the evidence.
4. **Allocator** turns the ranking into a prize plan (fixed tiers, split pools, ties) inside the budget.
5. **Human-in-the-loop approval** in an AG Grid dashboard (edit scores, exclude, re-run).
6. **PayPal Payouts API** (sandbox) sends a batch payout to winners' PayPal emails; status is polled /
   webhook-tracked per item (SUCCESS / UNCLAIMED / RETURNED) and failed items can be retried.
7. Optional: PayPal **Invoicing** for sponsors (fund-the-pool invoice), and the PayPal **MCP server / Agent Toolkit**
   so the agent can be driven from a chat client ("pay the top 10 from the Hazels campaign").

Why it fits the judging: real problem (the user lives this every week as a campaign participant), agentic commerce
(agent proposes payments, human approves, agent executes), central PayPal integration (OAuth, Payouts, webhooks,
Invoicing, MCP), AG Grid sponsor prize, safe-by-design (no auto-pay without approval, budget caps, idempotent
`sender_batch_id`).

## Stack

- Python 3.11, FastAPI, httpx, Pydantic v2; SQLite (stdlib) for state.
- Frontend: single static page + AG Grid Community (CDN), no build step.
- LLM: OpenAI-compatible client; providers `gemini` (AI Studio key), `groq`, `ollama` (local qwen2.5:3b), `mock`.
- PayPal: REST sandbox `https://api-m.sandbox.paypal.com` (OAuth2 client credentials, `/v1/payments/payouts`,
  `/v2/invoicing/invoices`), optional `@paypal/mcp`.
- Tests: pytest + respx (no network), `PAYPAL_MODE=mock` and `LLM_PROVIDER=mock` for CI.

## Milestones (IST)

| Date | Work |
| --- | --- |
| Oct 25–26 | Scaffold, integrity checks, allocator, PayPal client (mock + sandbox), tests (done in first session) |
| Oct 27–29 | LLM judge + reviewer, FastAPI endpoints, AG Grid dashboard |
| Oct 30–Nov 2 | Real sandbox run: OAuth, batch payout, status polling, webhooks, retry; Invoicing for sponsor funding |
| Nov 3–5 | MCP / Agent Toolkit path, sample campaign data (real public campaign briefs), polish UI |
| Nov 6–8 | Host demo (Render free tier or similar), README, architecture diagram |
| Nov 9–10 | Record < 3 min video, write Devpost description |
| Nov 11 | Buffer; user approves and submits on Devpost (before Nov 13 1:30 AM IST) |

## Steps that need a browser / the user

1. **Devpost**: register for the hackathon (Join) with the user's Devpost account (sign in with GitHub Surajss11 or
   Google surajssh87@gmail.com).
2. **PayPal Developer**: log in at https://developer.paypal.com with a PayPal account (create one with
   surajssh87@gmail.com if needed; PayPal may ask for a phone number by SMS). Then Apps & Credentials → Sandbox →
   Create App "PayoutPilot" → copy Client ID + Secret into a secure form (never in chat). Sandbox only, no money,
   no KYC needed for sandbox.
3. **LLM key** (pick one, free): Google AI Studio API key (https://aistudio.google.com/apikey, sign in with Google)
   or Groq (https://console.groq.com). Local Ollama works without any key but is slow.
4. **YouTube**: upload the demo video (public) from the user's channel at the end.
5. Final **Devpost submission** needs the user's explicit approval.

## Safety

Sandbox credentials only. No live PayPal money, no wallets, no signing, no KYC. Payouts require an explicit
approval call with a budget cap; every batch uses a deterministic `sender_batch_id` so a retry cannot double-pay.
