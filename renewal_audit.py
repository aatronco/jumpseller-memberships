"""Read-only order diagnostics. Candidate matches NEVER authorize membership access."""
import datetime as dt
import json
from memberships import now, timestamp, positive_id

FIELDS = 'id,source,created_at,completed_at,status,status_enum,customer,products'

class RenewalAuditor:
    def __init__(self, state, api, max_pages=20):
        self.state, self.api, self.max_pages = state, api, max_pages
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError('Positive page budget required')
        with state.lock, state.db:
            state.db.execute('''CREATE TABLE IF NOT EXISTS renewal_audits (
                subscription INTEGER PRIMARY KEY, last_attempt TEXT, last_success TEXT,
                remembered_until TEXT, result TEXT NOT NULL DEFAULT '{}')''')

    def _orders(self, customer):
        orders = {}
        for page in range(1, self.max_pages + 1):
            batch = self.api.request('GET', f'customers/{positive_id(customer)}/orders.json?limit=100&page={page}&fields={FIELDS}')
            if not isinstance(batch, list):
                raise ValueError('Unexpected order response')
            for wrapped in batch:
                order = wrapped['order']
                oid = positive_id(order['id'])
                # The server route is customer-scoped, but reject inconsistent identities.
                if order.get('customer', {}).get('id') != customer:
                    raise ValueError('Order customer mismatch')
                if oid in orders:
                    raise ValueError('Overlapping or inconsistent order pagination')
                orders[oid] = order
            if len(batch) < 100:
                return list(orders.values()), True
        return list(orders.values()), False

    def _diagnose(self, row, orders, complete, clock, boundary):
        plan = self.state.config.plans[row['plan']]
        candidates = []
        for order in orders:
            if not any(p.get('id') == plan['product_id'] and p.get('variant_id') == plan.get('variant_id') for p in order.get('products', [])):
                continue
            dates = [timestamp(order[k]) for k in ('created_at', 'completed_at') if order.get(k)]
            if not dates:
                continue
            # This is only a diagnostic window, never a payment-period calculation.
            anchor = boundary or clock
            if not any(anchor - dt.timedelta(days=2) <= d <= clock + dt.timedelta(minutes=5) for d in dates):
                continue
            status = order.get('status_enum') or str(order.get('status', '')).lower().replace(' ', '_')
            candidates.append({'order_id': order['id'], 'status': status,
                'recurring_source': isinstance(order.get('source'),dict) and order['source'].get('created_from') == 'product_subscription',
                'created_at': order.get('created_at'), 'completed_at': order.get('completed_at')})
        for c in candidates:
            c['evidence'] = 'subscription_generated_order' if c['recurring_source'] else 'recurrence_source_unconfirmed'
            c['paid_recurring_order'] = c['recurring_source'] and c['status']=='paid'
            c['subscription_match_confirmed'] = False
        paid_recurring = any(c['paid_recurring_order'] for c in candidates)
        paid = any(c['status'] == 'paid' for c in candidates)
        pending = any(c['status'] == 'pending_payment' for c in candidates)
        status = 'candidates_unverified' if paid else ('waiting_for_paid_order' if pending else 'no_candidate')
        if paid_recurring:status='paid_recurring_order_requires_review'
        if not complete:status='incomplete_order_scan'
        known_matches=[]
        for known in self.state.rows():
            known_plan=self.state.config.plans[known['plan']]
            if known['customer']==row['customer'] and (known_plan['product_id'],known_plan.get('variant_id'))==(plan['product_id'],plan.get('variant_id')):
                known_matches.append(known['id'])
        # Our event ledger is not an authoritative inventory: missing webhooks and
        # other subscriptions are possible, even when this list contains one ID.
        blockers=['subscription_inventory_not_authoritative','exact_subscription_join_unavailable','paid_period_unavailable']
        if len(known_matches)>1:blockers.append('multiple_known_subscriptions_for_product')
        if not complete:blockers.append('incomplete_order_scan')
        return {'subscription_id': row['id'], 'customer_id': row['customer'], 'status': status,
            'correlation': 'exact_join_unavailable', 'complete': complete,
            'paid_recurring_order_found':paid_recurring, 'known_subscription_ids':sorted(known_matches),
            'subscription_inventory_complete':False,'auto_renewal_blockers':blockers,
            'candidates': sorted(candidates, key=lambda c: c['order_id']),
            'benefits_changed': False, 'checked_at': clock.isoformat(),
            'phase': ('before' if clock < boundary else 'at_or_after') if boundary else 'unknown',
            'expected_renewal_at': boundary.isoformat() if boundary else None}

    def run_due(self, clock=None):
        clock = clock or now()
        if clock.tzinfo is None:
            raise ValueError('Aware clock required')
        results, cache = [], {}
        store_verified = False
        # Snapshot only; never hold state lock over remote network reads.
        for row in self.state.rows():
            with self.state.lock, self.state.db:
                old = self.state.db.execute('SELECT * FROM renewal_audits WHERE subscription=?', (row['id'],)).fetchone()
                remembered = row['valid_until'] or (old['remembered_until'] if old else None)
                boundary = timestamp(remembered) if remembered else None
                near = boundary is not None and abs((clock-boundary).total_seconds()) <= 86400
                interval = dt.timedelta(hours=1 if near else 24)
                previous = json.loads(old['result']) if old else {}
                if previous.get('complete') is False:
                    interval = dt.timedelta(minutes=5)
                boundary_changed = old is not None and remembered != previous.get('expected_renewal_at')
                if old and not boundary_changed and old['last_attempt'] and clock-timestamp(old['last_attempt']) < interval:
                    # Persist known lease even when no network audit is due.
                    self.state.db.execute('UPDATE renewal_audits SET remembered_until=? WHERE subscription=?', (remembered, row['id']))
                    continue
                self.state.db.execute('''INSERT INTO renewal_audits(subscription,last_attempt,remembered_until)
                    VALUES(?,?,?) ON CONFLICT(subscription) DO UPDATE SET last_attempt=excluded.last_attempt, remembered_until=excluded.remembered_until''',
                    (row['id'], clock.isoformat(), remembered))
            try:
                if not store_verified:
                    info = self.api.request('GET', 'store/info.json')
                    if info['store']['code'] != self.state.config.store:
                        raise ValueError('Wrong API store')
                    store_verified = True
                if row['customer'] not in cache:
                    cache[row['customer']] = self._orders(row['customer'])
                orders, complete = cache[row['customer']]
                result = self._diagnose(row, orders, complete, clock, boundary)
            except Exception as exc:
                # Never expose exception messages: upstream errors may contain private data.
                result = {'subscription_id': row['id'], 'customer_id': row['customer'],
                    'status': 'audit_failed', 'error_type': type(exc).__name__, 'complete': False,
                    'correlation': 'exact_join_unavailable', 'benefits_changed': False,
                    'checked_at': clock.isoformat(),
                    'phase': ('before' if clock < boundary else 'at_or_after') if boundary else 'unknown',
                    'expected_renewal_at': boundary.isoformat() if boundary else None}
            with self.state.lock, self.state.db:
                self.state.db.execute('UPDATE renewal_audits SET result=?, last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE subscription=?',
                    (json.dumps(result), result['complete'], clock.isoformat(), row['id']))
            results.append(result)
        return results

    def results(self):
        with self.state.lock:
            return [json.loads(r['result']) for r in self.state.db.execute('SELECT result FROM renewal_audits ORDER BY subscription')]
