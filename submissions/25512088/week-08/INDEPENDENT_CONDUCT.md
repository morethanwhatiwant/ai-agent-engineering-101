# Round 3 independent-performance conduct

Deployment audit started 2026-11-05 16:18 KST. This file separates code, tests,
deployment and measured market outcome; passing tests are not called deployment.

## Buyer

- Implemented: the market order is used unchanged. `restrict_order` and its food
  exclusion are no longer in the execution path. Every acceptable SKU offered by
  every eligible non-self shop is enumerated. Ranked waves use at most six network
  calls at once and stop at the order/85-second deadline.
- Implemented: hard budget and grade-specific price caps, expiry and food
  `use_within_hours`, goods 6h warranty, registered signing key, signed task/SKU/
  amount, same-owner exclusion, one receipt and uncertain-payment stop remain.
- Implemented: ranking records absolute expected surplus and estimated valid
  checkout probability. Food 5% and goods 15% breakage are explicitly labelled
  `conservative-unmeasured`; no warranty recovery is credited before a matured
  cohort exists. These are ranking assumptions, not measured rates.
- Implemented: `no_stock`, `no_quote`, `condition_rejected`, `price_rejected`,
  `network_error` and `protocol_error` are separate durable events. Cooldown and
  new stock/order evidence can reopen a rival; uncertain payment cannot.
- Preserved: the post-payment hook writes the actual conditions and ledger-backed
  factual review. Forum output is factual price/condition or actual purchase data,
  rate-limited, and does not auto-vote or fabricate a review. Buyer weights are
  surplus 50%, forum 30%, deals 10%, posts 10%.

## Seller

- Implemented: goods-only TOP10 v3 runs from 180 seconds before each supply
  opening. It records 1h/6h exact grade+6h honest trades, identifiable distinct
  buyers, current-alive/recent-active buyer evidence, own inquiries and stockouts.
  Our buyer orders are labelled biased personal evidence, never obligations.
- Implemented: competitor same SKU and same-category 50-150% price-band
  substitute inventory both reduce likelihood. Ranking is estimated sale
  likelihood times net contribution/capital; high price is not a gate and
  negative expected contribution is never forced into TOP10.
- Implemented: A/B/C arrival probabilities are 70/20/10 and acquisition ranges
  are A 55-75%, B 40-60%, C 25-45%. The 15% hazard prior is explicitly
  conservative/unmeasured. Missing grade trade evidence is costed as unsold.
- Implemented: allocation uses current holdings, 1h/6h sell-through, unmet demand
  and protected capital. There is no fixed-four loop or per-SKU unit cap. The
  2.5m limit is current unsold inventory cost and reopens after official inventory
  removal. Cash protection remains 500k plus warranty, credits and unpaid rewards.
- Implemented: an unknown/5xx supply POST or an invalid 200 cost stops for durable
  reconciliation; GETs retain bounded retry. Pricing combines same-condition
  settlements, actual quote/sale conversion and same/substitute competition.
  Scarcity alone cannot raise price. Cost, benefit, warranty and prior lower quote
  protections remain.
- Preserved: Don Quixote, honest actual A/B/C receipt claim before reservation,
  signed receipts, unit reservation, no self-trade/double sale, real-stock OFFERS,
  existing discounts/referrals and frozen promises. Referral parsing by rivals is
  not assumed and campaign state is not reset.

## Evidence boundaries

- Live public reads attempted with 12-second bounds at 2026-11-05 16:12 KST for
  `/agent.md`, `/api/board`, `/api/shops`, `/api/ledger?after=-1000`,
  `/api/forum?limit=100`, and `/api/demand?limit=100`; the endpoint timed out from
  this shell. No zero-market conclusion is drawn.
- The already running authenticated processes continued refreshing public-derived
  files: demand 16:18:46, forum 16:18:44 and market log 16:18:45 KST. Shops last
  refreshed 16:10:54 before the market began returning no-shop responses. The
  latest cached board source was 16:07:29 KST and showed the buyer alive.
- `strategy.json` v3 stores `selected_at`, `ledger_through`, `ledger_after=-1000`,
  returned row count and 900-second freshness bound. Pagination/freshness is
  evidence metadata, not a completeness claim.
- Tested: unit/integration tests cover food and complete-SKU restoration,
  alternative shops/SKUs, conservative expected surplus, caps and certificate
  checks, uncertain stop, outcome taxonomy, sold inventory capital release,
  active/distinct demand, substitute competition, positive-net allocation,
  read failure recovery, honest claims and protected pricing.
- Deployment and measured outcome are recorded only after process restart and
  fresh worker/order/card evidence below. No fake purchase is issued.

## Deployment record

Pending at document creation. This section must be updated with PID/cwd/runtime,
restart time, first new buyer order, public card/inventory freshness and seller
worker v3 event. Market performance requires later real orders/sales and is not
inferred from tests.
