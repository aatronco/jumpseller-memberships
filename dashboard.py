"""Read-only operator view. Never derives entitlement from order candidates."""
import html
from memberships import now

def snapshot(state,reconciler,auditor,clock=None):
    clock=clock or now(); audits=auditor.results(); by_id={a.get('subscription_id'):a for a in audits}
    members=[]
    for row in state.rows():
        plan=state.config.plans[row['plan']]; r=dict(row);r.update(state.entitlement(row,clock))
        r.update(category_id=plan['category_id'],category_name=plan['category_name'],promotion_id=plan['promotion_id'],
                 period='Monthly' if plan['period']==30 else 'Annual',
                 promotion=getattr(reconciler.api,'verified_promotions',{}).get(plan['promotion_id']),
                 category_present=row['customer'] in reconciler.observed.get(row['plan'],[]) if row['plan'] in reconciler.observed else None,
                 evidence=state.evidence(row['id']),renewal_audit=by_id.get(row['id']))
        members.append(r)
    return {'members':members,'active':sorted(state.active(clock)),'live':state.config.live,
            'grace_days':state.config.grace_days,'last_sync':reconciler.last_success,'sync_error':reconciler.error,
            'renewal_audits':audits,'checked_at':clock.isoformat()}

def esc(x):return html.escape(str(x if x is not None else 'Not yet available'))
def render(data):
    cards=[]
    for r in data['members']:
        audit=r['renewal_audit'] or {};promo=r['promotion'] or {}
        events=''.join('<li><time>'+esc(e['event_time'])+'</time> · '+esc(e['kind'])+'<br><code>'+esc(e['detail'])+'</code></li>' for e in r['evidence']) or '<li>No event evidence recorded.</li>'
        candidates=''.join('<li>Order #'+esc(o['order_id'])+' · '+esc(o['status'])+' · recurring source: '+esc(o['recurring_source'])+'</li>' for o in audit.get('candidates',[]))
        cards.append(f'''<article><header><h2>Subscription #{r['id']} <small>Customer #{r['customer']}</small></h2><b class="{esc(r['benefit_state'])}">{esc(r['benefit_state']).upper()}</b></header>
        <div class="grid"><section><label>Plan</label><strong>{esc(r['period'])}</strong><label>Paid through</label>{esc(r['paid_through'])}<label>Grace ends</label>{esc(r['grace_until'])}<label>Native status / updated</label>{esc(r['status'])} / {esc(r['version'])}</section>
        <section><label>Benefit</label><strong>{'Enabled by app policy' if r['benefit_active'] else 'Not entitled'}</strong><label>Customer category</label>{esc(r['category_name'])} (#{r['category_id']})<label>Category observed at last sync</label>{'Present' if r['category_present'] else ('Absent' if r['category_present'] is False else 'Not checked')}<label>Promotion #{r['promotion_id']}</label>{esc(promo.get('name'))}<br>Discount: {esc(promo.get('discount_amount_percent') if promo.get('type')=='percentage' else promo.get('discount_amount_fix'))} {'%' if promo.get('type')=='percentage' else esc(promo.get('type'))}</section></div>
        <section class="review"><label>Renewal evidence</label>{esc(audit.get('status','Not checked'))} · Last check: {esc(audit.get('checked_at'))}<br>Expected renewal: {esc(audit.get('expected_renewal_at'))}<ul>{candidates}</ul><small>Candidate orders require review. A product/customer match is not proof of this subscription’s paid period.</small></section>
        <details><summary>Activation and renewal evidence ({len(r['evidence'])} recent events)</summary><ul>{events}</ul></details></article>''')
    active=sum(r['benefit_state']=='active' for r in data['members']);grace=sum(r['benefit_state']=='grace' for r in data['members'])
    body=''.join(cards) or '<article><h2>No subscriptions received yet</h2><p>This is the real store panel, not sample data. After a test payment, the signed subscription event will appear here.</p></article>'
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30"><title>Membership operations</title><style>
    *{box-sizing:border-box}body{font:15px/1.5 system-ui;background:#f2f5f8;color:#14233b;max-width:1160px;margin:auto;padding:32px}h1{font-size:32px;margin-bottom:4px}h2{font-size:20px;margin:0}small,time{color:#576a7c}header{display:flex;gap:12px;justify-content:space-between;align-items:center}h2 small{display:block;font-size:14px;font-weight:400}.metrics,.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.metrics{grid-template-columns:repeat(3,1fr);margin:24px 0}.metrics div,article{background:white;padding:24px;border:1px solid #dbe3eb;border-radius:12px}article{margin-bottom:20px}.metrics strong{display:block;font-size:28px}label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#576a7c;margin-top:16px}.active{color:#12684e}.grace{color:#966000}.expired,.suspended{color:#a63333}.review,details{border-top:1px solid #e0e7ee;margin-top:20px;padding-top:12px}code{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:10px 0}.health{padding:12px;background:#e7eff7;border-radius:8px}@media(max-width:640px){body{padding:16px}.grid,.metrics{grid-template-columns:1fr}header{align-items:start}}
    </style><h1>Membership operations</h1><p>Paid periods, customer-category benefits and renewal evidence. Refreshes every 30 seconds.</p>'''+f'''<div class="health">{'LIVE STORE SYNC' if data['live'] else 'DRY RUN · No store writes'} · Last category sync: {esc(data['last_sync'])} · Error: {esc(data['sync_error'] or 'None')}</div><div class="metrics"><div><strong>{active}</strong>Active paid periods</div><div><strong>{grace}</strong>In {data['grace_days']}-day grace</div><div><strong>{len(data['members'])}</strong>Recorded subscriptions</div></div>'''+body+'<p><small>Daily order review is automatic while the app runs. An authenticated native event is required to confirm a renewal. Grace retains benefits; it does not charge the customer. No customer emails are sent.</small></p></html>'
