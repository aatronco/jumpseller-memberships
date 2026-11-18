# Jumpseller Memberships

A small self-hosted membership-benefit app for Jumpseller. Native Jumpseller product subscriptions collect monthly or annual payments; this app maintains dedicated customer categories that activate order-subtotal promotions.

**Beta: functional integration, not a payment processor or a complete subscription accounting system.** No cards, payment credentials, charging endpoints or customer email sending. Dry-run is the default.

## What it does

- Authenticated member dashboard: subscription/customer IDs, plan and benefit expiration.
- Native monthly (30) and annual (365) recurrence selectors, configurable product/variant mappings.
- Signed subscription webhooks → durable SQLite state → category API synchronization.
- Benefits remain active through the native paid-through timestamp plus five days of renewal grace (configurable `grace_days`, default 5). Grace is not a new paid period.
- Duplicate/reordered delivery handling, conflicting-version quarantine, signed V2 timestamp validation.
- Continuous reconciliation, write read-back verification, and an emergency suspension endpoint.
- Only configured app-exclusive categories are managed; other categories remain untouched.

A customer must **log in** to receive a category-restricted promotion. Entering a member's email as a guest is not enough. Benefit activation is asynchronous: after payment, wait for Jumpseller's dated subscription webhook plus a successful category write. This is not a guarantee of zero-latency checkout eligibility.

## Quick start

Requires Python 3.12+ or Docker Compose. No Python dependencies.

1. Create a digital subscription product in Jumpseller with monthly and annual frequencies. Product prices and commercial terms are your responsibility.
2. Create **new, empty, app-exclusive** customer categories prefixed `Membership App / `. Never point the app at an existing shared customer group: reconciliation removes members not present in this app's state.
3. Create one noncumulative percentage promotion per plan, restricted to its dedicated customer category and targeting the **order subtotal** (`discount_target: "order"`, `products: []`, `categories: []`). Example commercial settings are monthly 10% and annual 15%; configure your own rates in Jumpseller. Shipping is not part of the order subtotal. The app maintains membership categories; it does not create or overwrite promotions. Verify membership-product and native recurring-charge treatment separately before production; do not assume ordinary order promotions discount recurring billing.
4. Copy `config.example.json` to `config.json` and enter the real store/product/category/promotion IDs. Keep `live: false` initially. Variants must match exactly; use `null` for an unvaried product.
5. Copy `.env.example` to `.env`; set the store's API login/token, webhook secret and a strong random admin token. Do not commit or expose these values.
6. `docker compose up --build -d`
7. Open `http://127.0.0.1:4248`. Username `admin`, password is `ADMIN_TOKEN`.
8. Put a trusted HTTPS reverse proxy in front of the app. Rate-limit requests, cap request bodies at 128 KiB, enforce timeouts, and restrict dashboard/admin endpoints to your VPN/IP or another access layer. Expose only `/webhooks/subscriptions` publicly if possible. Do not expose the raw Python HTTP listener directly to the internet.
9. Register subscription-created, subscription-updated and subscription-canceled webhooks to `https://YOUR-HOST/webhooks/subscriptions`. Use the same store's hook secret. Current V2 signed headers are required; unsigned/legacy-only requests are rejected.
10. Complete the acceptance checklist below, enable intended test promotions, set `live: true`, restart. Watch dashboard sync health.

Without Docker:

```sh
# Export environment values from a secure local source first.
python -m unittest discover -s tests -v
python memberships.py
```

Default bind is loopback `127.0.0.1:4248`. `CONFIG_FILE`, `DATABASE_FILE`, `HOST` and `PORT` are configurable. One process owns a database, enforced by an OS lock. Run only one instance per store/database. SQLite data is persistent; the desired state is retried every 30 seconds and immediately after webhook acceptance. Keep the service running for expiration handling.

## Entitlement and failure rules

- Only a configured product/variant/period with **signed `active` plus a future `next_order_date`** grants access. `active` without a date is accepted without benefit; a subsequent dated update can activate it. The original timestamp is the lease boundary: replays never add time.
- Native month/year semantics are calendar-based. The app uses the authoritative timestamp, not its own 30/365-day arithmetic.
- `pending` never grants a new lease, but retains an already recorded paid period and its five-day grace. At the grace deadline access expires unless a newer authenticated dated subscription event confirms renewal.
- Cancellation preserves an already-recorded paid lease until its original expiration (no renewal grace); a cancellation event by itself never creates a lease.
- Equal `updated_at` with conflicting entitlement fields (except the native null-to-dated initial activation), or identity/plan changes on the same subscription ID, quarantine the subscription. Access is suspended and must be investigated. Do not simply replay a larger date to clear quarantine.
- Multiple valid subscriptions can grant multiple plan categories; promotions are required noncumulative. Validate which promotion wins for your pricing model before live use.
- Refunds/reversals of renewal orders may not emit a corresponding subscription change. **Automatic immediate refund revocation is not supported.** Use emergency suspension and investigate; reliable automatic refunds require a verified order-to-subscription linkage.
- No public native subscription-list API is assumed. Missed activation webhooks cannot be reconstructed from customer/product guesses. Recover through a verified resend or authoritative integration; do not manufacture active status.
- Downtime/API failures can delay grant or revoke. Monitor sync health and revoke permissions/disable promotions manually for a prolonged outage. This app does not claim checkout-atomic expiry.
- Changing store/plan mappings on an existing database requires controlled migration. Never reuse a database for another store.

## Security and operations

Webhook V2 verifies HMAC-SHA256 over `timestamp.delivery_id.raw_body`, with a 5-minute clock tolerance. Keep the host's clock synchronized. Persisted delivery IDs and body versions protect against duplicate retries and old-body replay. Secrets stay server-side. Stored member data is limited to IDs and entitlement state; incoming email/address fields are not persisted. Logs omit headers, secrets and raw payloads.

The dashboard uses HTTP Basic authentication, so HTTPS is mandatory beyond loopback. Protect `.env`, config, volume and backups. Back up SQLite using its backup API, not a live file copy that omits WAL files. Keep delivery/audit history within your retention policy; no automatic pruning is currently provided.

Emergency suspension (administrator only): `POST /api/suspend`, HTTP Basic credentials, header `X-Admin-Action: suspend`, JSON `{"subscription_id": 1001}`. This quarantines the subscription and schedules group removal; other valid subscriptions for the same customer may still confer benefits. There is intentionally no unaudited grant endpoint.

`GET /healthz` checks process liveness only. Authenticated `GET /api/members` reports last successful synchronization and error type. Monitor stale `last_sync` and non-null `sync_error`, not just liveness.

## Acceptance checklist

- Correct store verified; plan selectors and dedicated category/promotions verified.
- Fresh manual test purchase → pending, no benefit → mark paid in test store → dated native webhook → category → ordinary product discount for logged-in member.
- Guest using member email, unrelated customer, expired membership: no membership discount.
- Renewal success, failed renewal/expiry, cancellation, duplicate delivery, out-of-order delivery, malformed signature, restart and API outage.
- Category add/remove read-back; no other categories modified.
- Refund suspension workflow and operator recovery documented.
- Restore database, service restart and reverse-proxy access controls tested.

Unit tests exercise state/authentication/synchronization with synthetic fixtures. Passing them does not prove payment-provider integration or logged-in checkout behavior. Run the checklist in your test store before production.

## References

- [Jumpseller subscriptions](https://jumpseller.com/support/product-subscriptions/)
- [Jumpseller webhooks](https://jumpseller.com/support/webhooks/)
- [Official API](https://api.jumpseller.com/)

Independent integration. No Jumpseller proprietary source, real merchant data or credentials are included.


## Read-only renewal order audit
The app checks known customers' orders daily and hourly within 24 hours before/after the last known renewal boundary. A separate worker prevents long order reads from delaying category expiry. Results appear in the authenticated dashboard and `/api/members` under `renewal_audits`.

Matching product/variant and a paid order only produces an **unverified candidate**. Public order data does not provide the subscription ID or authoritative paid-through period, so this audit NEVER grants or extends benefits. Pending orders do not count as payment. Incomplete pagination and API errors are explicit. This helps detect potential missed events but is not automatic recovery of authoritative subscription state.

## Operator panel and grace
The real read-only panel shows paid-through, grace deadline, native state/version, category presence at last synchronization, API-verified promotion metadata and the last 20 audited events. Empty state means no subscriptions have been received; it never creates demonstration memberships. Category presence is a last-observed value, not a checkout guarantee. The order worker checks daily (hourly near the boundary) and retries incomplete reads; the reconciliation worker enforces the grace deadline. Both require the app process to remain running. No scheduler on a sleeping/offline Mac can guarantee timely execution.

No public order-to-subscription join has been verified. Therefore daily order checks flag paid candidates for review, rather than silently extending the wrong membership. Renewal through authenticated native subscription events remains automatic. This limitation is shown in the panel; do not describe this version as automatic order-based recovery.

### Optional password-free local reading
Set `allow_local_readonly: true` only for a trusted personal computer bound to loopback. Dashboard GETs then accept loopback clients with exact localhost Host and same-origin/absent Origin, refusing forwarded and cross-site requests. Other local processes/users on that computer can read member IDs and state. Mutating admin actions still require authentication. Never proxy the dashboard publicly; expose only the signed webhook route. Default remains authenticated.

## Verification scope
Run `python -m unittest discover -s tests -v` and `python -m py_compile memberships.py renewal_audit.py dashboard.py`. Tests use synthetic IDs and local HTTP servers; they do not place orders or call a payment provider. Coverage includes grace boundaries, renewal events, duplicate/out-of-order/conflicting webhooks, cancellation, category write verification, store mismatch, pagination/API failures, dashboard evidence, HMAC/authentication and localhost origin/host guards. Real annual payment, real recurring collections, refunds, months-long scheduling and production deployment remain separate acceptance checks.

### Positive recurrence evidence, conservative matching
`source.created_from == "product_subscription"` confirms that Jumpseller generated the order from a subscription; it is not a manual-checkout guess. A paid order with this source is marked `paid_recurring_order_requires_review`. This source alone still does not expose the subscription ID or paid-through period. Multiple subscriptions for the same customer/product/variant can exist, and the local webhook ledger is not a complete native subscription inventory. Even one locally known subscription therefore does not authorize automatic renewal. The panel reports known matching IDs, inventory incompleteness, and precise blockers. Incomplete scans, overlapping pages and contradictory duplicate orders never establish a unique match or extend a lease. Automatic renewals continue through authenticated native dated subscription events.
