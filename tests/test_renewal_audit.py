import datetime as dt
import unittest
from memberships import Config, State, timestamp
from renewal_audit import RenewalAuditor

CLOCK=timestamp('2026-11-16T12:00:00Z')
class API:
    def __init__(self, batches=None): self.calls=[]; self.batches=batches or [[]]
    def request(self, method, path):
        self.calls.append((method,path))
        if path == 'store/info.json':return {'store': {'code':'example'}}
        page=int(path.split('page=')[1].split('&')[0]);return self.batches[page-1]
def order(oid=1,status='paid',source='product_subscription'):
    return {'order':{'id':oid,'customer':{'id':42},'products':[{'id':1,'variant_id':None}], 'status_enum':status,'created_at':CLOCK.isoformat(),'completed_at':CLOCK.isoformat(),'source':{'created_from':source}}}
class AuditTests(unittest.TestCase):
    def setUp(self):
        c=Config({'store':'example','grace_days':0,'live':False,'plans':{'monthly':{'product_id':1,'period':30,'category_id':10,'promotion_id':20,'category_name':'Membership App / Monthly'}}})
        self.s=State(':memory:',c)
        self.s.db.execute('INSERT INTO subscriptions VALUES(1001,42,\'monthly\',?,\'test\',\'active\',?,0)',(CLOCK.isoformat(),(CLOCK+dt.timedelta(days=10)).isoformat()))
        self.s.db.commit()
    def tearDown(self):self.s.db.close()
    def test_daily(self):
        a=RenewalAuditor(self.s,API());self.assertEqual(1,len(a.run_due(CLOCK)));self.assertEqual([],a.run_due(CLOCK+dt.timedelta(hours=23)));self.assertEqual(1,len(a.run_due(CLOCK+dt.timedelta(days=1))))
    def test_near_boundary_hourly(self):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',((CLOCK+dt.timedelta(hours=2)).isoformat(),));self.s.db.commit()
        a=RenewalAuditor(self.s,API());a.run_due(CLOCK);self.assertEqual([],a.run_due(CLOCK+dt.timedelta(minutes=59)));self.assertEqual(1,len(a.run_due(CLOCK+dt.timedelta(hours=1))))
    def test_remember_boundary_when_pending(self):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',((CLOCK+dt.timedelta(hours=2)).isoformat(),));self.s.db.commit()
        a=RenewalAuditor(self.s,API([[order(status='pending_payment')]]));a.run_due(CLOCK)
        self.s.db.execute("UPDATE subscriptions SET status='pending',valid_until=NULL");self.s.db.commit()
        result=a.run_due(CLOCK+dt.timedelta(hours=3))[0];self.assertEqual('waiting_for_paid_order',result['status']);self.assertFalse(self.s.active(CLOCK))
    def test_paid_candidate_does_not_extend(self):
        before=self.s.rows();a=RenewalAuditor(self.s,API([[order()]]));r=a.run_due(CLOCK+dt.timedelta(days=10))[0]
        self.assertEqual(before,self.s.rows());self.assertFalse(r['benefits_changed']);self.assertFalse(self.s.active(CLOCK+dt.timedelta(days=10)))
    def test_paid_near_boundary_unverified(self):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',(CLOCK.isoformat(),));self.s.db.commit()
        a=RenewalAuditor(self.s,API([[order()]]));r=a.run_due(CLOCK)[0];self.assertEqual('paid_recurring_order_requires_review',r['status']);self.assertEqual('exact_join_unavailable',r['correlation'])
    def test_pagination_and_incomplete(self):
        a=RenewalAuditor(self.s,API([[order(i) for i in range(1,101)],[]]));a.run_due(CLOCK);self.assertEqual(3,len(a.api.calls));self.assertTrue(a.results()[0]['complete'])
        b=RenewalAuditor(self.s,API([[order(i) for i in range(1,101)]]),max_pages=1);r=b.run_due(CLOCK+dt.timedelta(days=1))[0];self.assertFalse(r['complete'])
    def test_checkout_candidate_not_mistaken_for_recurring(self):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',(CLOCK.isoformat(),));self.s.db.commit()
        a=RenewalAuditor(self.s,API([[order(source='checkout')]]));r=a.run_due(CLOCK)[0]
        self.assertEqual('candidates_unverified',r['status']);self.assertFalse(r['candidates'][0]['recurring_source']);self.assertFalse(self.s.active(CLOCK))
    def test_variant_mismatch_not_candidate(self):
        o=order();o['order']['products'][0]['variant_id']=9
        a=RenewalAuditor(self.s,API([[o]]));self.assertEqual('no_candidate',a.run_due(CLOCK)[0]['status'])
    def test_wrong_store_blocks_customer_reads(self):
        api=API();api.request=lambda method,path: {'store':{'code':'wrong'}} if path=='store/info.json' else self.fail('Customer read in wrong store')
        a=RenewalAuditor(self.s,api);self.assertEqual('audit_failed',a.run_due(CLOCK)[0]['status'])
    def test_incomplete_retry_five_minutes(self):
        a=RenewalAuditor(self.s,API([[order(i) for i in range(1,101)]]),max_pages=1)
        self.assertFalse(a.run_due(CLOCK)[0]['complete']);self.assertEqual([],a.run_due(CLOCK+dt.timedelta(minutes=4)))
        self.assertEqual(1,len(a.run_due(CLOCK+dt.timedelta(minutes=5))))
    def test_phase(self):
        a=RenewalAuditor(self.s,API());r=a.run_due(CLOCK)[0]
        self.assertEqual('before',r['phase']);self.assertEqual((CLOCK+dt.timedelta(days=10)).isoformat(),r['expected_renewal_at'])
        self.assertEqual('at_or_after',a.run_due(CLOCK+dt.timedelta(days=10))[0]['phase'])
    def test_error_retry_five_minutes(self):
        api=API();api.request=lambda *args: (_ for _ in ()).throw(ValueError('error'))
        a=RenewalAuditor(self.s,api);self.assertEqual('audit_failed',a.run_due(CLOCK)[0]['status'])
        self.assertEqual([],a.run_due(CLOCK+dt.timedelta(minutes=4)));self.assertEqual(1,len(a.run_due(CLOCK+dt.timedelta(minutes=5))))
    def test_wrong_customer_fails(self):
        o=order();o['order']['customer']['id']=99;a=RenewalAuditor(self.s,API([[o]]));self.assertEqual('audit_failed',a.run_due(CLOCK)[0]['status'])
    def test_naive_dates_fail_closed(self):
        o=order();o['order']['created_at']='2026-11-16T12:00:00';a=RenewalAuditor(self.s,API([[o]]));self.assertEqual('audit_failed',a.run_due(CLOCK)[0]['status'])
    def test_no_private_fields_saved(self):
        o=order();o['order']['customer']['email']='private@example.invalid';a=RenewalAuditor(self.s,API([[o]]));a.run_due(CLOCK);self.assertNotIn('private',str(a.results()))
if __name__=='__main__':unittest.main()

class BoundaryRefresh(unittest.TestCase):
    setUp=AuditTests.setUp
    tearDown=AuditTests.tearDown
    def test_new_native_boundary_refreshes_immediately(self):
        a=RenewalAuditor(self.s,API());a.run_due(CLOCK)
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',((CLOCK+dt.timedelta(days=40)).isoformat(),));self.s.db.commit()
        result=a.run_due(CLOCK+dt.timedelta(seconds=1))
        self.assertEqual(1,len(result));self.assertEqual((CLOCK+dt.timedelta(days=40)).isoformat(),result[0]['expected_renewal_at'])

class RecurrenceEvidence(unittest.TestCase):
    setUp=AuditTests.setUp
    tearDown=AuditTests.tearDown
    def check(self,o=None):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',(CLOCK.isoformat(),));self.s.db.commit()
        a=RenewalAuditor(self.s,API([[o or order()]]))
        with self.s.db:self.s.db.execute('DELETE FROM renewal_audits')
        return a.run_due(CLOCK)[0]
    def test_paid_recurrence_positive_evidence_not_lease(self):
        before=self.s.rows()[0]['valid_until'];r=self.check()
        self.assertTrue(r['paid_recurring_order_found']);self.assertTrue(r['candidates'][0]['paid_recurring_order'])
        self.assertEqual('subscription_generated_order',r['candidates'][0]['evidence'])
        self.assertFalse(r['benefits_changed']);self.assertFalse(self.s.active(CLOCK))
    def test_single_known_subscription_not_proof_of_uniqueness(self):
        r=self.check();self.assertEqual([1001],r['known_subscription_ids']);self.assertFalse(r['subscription_inventory_complete'])
        self.assertIn('subscription_inventory_not_authoritative',r['auto_renewal_blockers']);self.assertFalse(r['candidates'][0]['subscription_match_confirmed'])
    def test_multiple_known_subscriptions_mark_ambiguity(self):
        self.s.db.execute("INSERT INTO subscriptions SELECT 1002,customer,plan,version,digest,status,valid_until,quarantine FROM subscriptions WHERE id=1001");self.s.db.commit()
        r=self.check();self.assertEqual([1001,1002],r['known_subscription_ids']);self.assertIn('multiple_known_subscriptions_for_product',r['auto_renewal_blockers'])
    def test_unpaid_recurrence_is_not_paid(self):
        r=self.check(order(status='pending_payment'));self.assertTrue(r['candidates'][0]['recurring_source']);self.assertFalse(r['paid_recurring_order_found']);self.assertEqual('waiting_for_paid_order',r['status'])
    def test_manual_paid_not_recurrence(self):
        r=self.check(order(source='checkout'));self.assertFalse(r['paid_recurring_order_found']);self.assertEqual('recurrence_source_unconfirmed',r['candidates'][0]['evidence'])
    def test_missing_source_does_not_confirm_recurrence(self):
        for source in (None,{},'product_subscription'):
            o=order();o['order']['source']=source;r=self.check(o);self.assertFalse(r['paid_recurring_order_found'])
    def test_refunded_recurrence_is_not_paid(self):
        r=self.check(order(status='refunded'));self.assertFalse(r['paid_recurring_order_found']);self.assertFalse(r['benefits_changed'])
    def test_partial_pages_do_not_prove_match(self):
        self.s.db.execute('UPDATE subscriptions SET valid_until=?',(CLOCK.isoformat(),));self.s.db.commit()
        r=RenewalAuditor(self.s,API([[order(i) for i in range(1,101)]]),max_pages=1).run_due(CLOCK)[0]
        self.assertEqual('incomplete_order_scan',r['status']);self.assertTrue(r['paid_recurring_order_found']);self.assertFalse(r['complete']);self.assertFalse(r['benefits_changed'])
    def test_overlapping_pages_fail_closed(self):
        r=RenewalAuditor(self.s,API([[order(i) for i in range(1,101)],[order(1)]])).run_due(CLOCK)[0]
        self.assertEqual('audit_failed',r['status']);self.assertFalse(r['complete'])
    def test_conflicting_same_order_fails_closed(self):
        r=RenewalAuditor(self.s,API([[order(1),order(1,status='refunded')]])).run_due(CLOCK)[0]
        self.assertEqual('audit_failed',r['status']);self.assertFalse(r['benefits_changed'])
