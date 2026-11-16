# Jumpseller Memberships

A small self-hosted membership-benefit app for Jumpseller. Native Jumpseller product subscriptions collect monthly or annual payments; this app maintains dedicated customer categories that activate merchandise promotions.

**Beta: functional integration, not a payment processor or a complete subscription accounting system.** No cards, payment credentials, charging endpoints or customer email sending. Dry-run is the default.

## What it does

- Authenticated member dashboard: subscription/customer IDs, plan and benefit expiration.
- Native monthly (30) and annual (365) recurrence selectors, configurable product/variant mappings.
- Signed subscription webhooks → durable SQLite state → category API synchronization.
- Benefits expire at the exact native `next_order_date`, even if subscription status stays active while renewal payment is pending.
- Duplicate/reordered delivery handling, conflicting-version quarantine, signed V2 timestamp validation.
- Continuous reconciliation, write read-back verification, and an emergency suspension endpoint.
- Only configured app-exclusive categories are managed; other categories remain untouched.

A customer must **log in** to receive a category-restricted promotion. Entering a member's email as a guest is not enough. Benefit activation is asynchronous: after payment, wait for Jumpseller's dated subscription webhook plus a successful category write. This is not a guarantee of zero-latency checkout eligibility.

## Quick start

Requires Python 3.12+ or Docker Compose. No Python dependencies.

1. Create a digital subscription product in Jumpseller with monthly and annual frequencies. Product prices and commercial terms are your responsibility.
2. Create **new, empty, app-exclusive** customer categories prefixed `Membership App / `. Never point the app at an existing shared customer group: reconciliation removes members not present in this app's state.
3. Create a promotion per plan restricted to its category, noncumulative, and eligible merchandise only. Exclude the membership product itself. Native recurring subscription charges do not accept normal promotions.
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
- `pending` suspends access. Expiry suspends access even if native status still says active.
- Cancellation preserves an already-recorded paid lease until its original expiration; a cancellation event by itself never creates a lease.
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
