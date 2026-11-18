import datetime as dt, unittest
from memberships import Config,State,Reconciler,timestamp
import test_memberships as fixtures
from test_memberships import configuration,event,FakeAPI,CLOCK
from dashboard import snapshot,render
from renewal_audit import RenewalAuditor
class Grace(unittest.TestCase):
    receive=fixtures.Events.receive
    def setUp(self):
        c=configuration();c.grace_days=5;self.s=State(':memory:',c)
    def test_grace_boundaries(self):
        self.receive(event(next_order_date=CLOCK.isoformat()))
        self.assertEqual('grace',self.s.entitlement(self.s.rows()[0],CLOCK)['benefit_state'])
        self.assertTrue(self.s.active(CLOCK+dt.timedelta(days=5,microseconds=-1)))
        self.assertFalse(self.s.active(CLOCK+dt.timedelta(days=5)))
    def test_pending_keeps_paid_period_and_grace(self):
        self.receive(event(next_order_date=CLOCK.isoformat()))
        self.receive(event(status='pending',next_order_date=None,updated_at='2026-11-16T11:30:00Z'))
        self.assertTrue(self.s.active(CLOCK+dt.timedelta(days=4)))
        self.assertFalse(self.s.active(CLOCK+dt.timedelta(days=5)))
    def test_new_pending_does_not_get_grace(self):
        self.receive(event(status='pending',next_order_date=None));self.assertFalse(self.s.active(CLOCK))
    def test_cancel_has_no_unpaid_grace(self):
        self.receive(event(next_order_date=CLOCK.isoformat()));self.receive(event(status='canceled',next_order_date=None,updated_at='2026-11-16T11:30:00Z'));self.assertFalse(self.s.active(CLOCK))
    def test_suspension_overrides_grace(self):
        self.receive(event(next_order_date=CLOCK.isoformat()));self.s.suspend(1001);self.assertFalse(self.s.active(CLOCK))
    def test_renewal_replaces_boundary_never_adds_grace_to_paid_period(self):
        self.receive(event(next_order_date=CLOCK.isoformat()));self.receive(event(next_order_date='2026-12-16T12:00:00Z',updated_at='2026-11-16T11:30:00Z'))
        r=self.s.entitlement(self.s.rows()[0],CLOCK);self.assertEqual('2026-12-16T12:00:00+00:00',r['paid_through']);self.assertEqual('2026-12-21T12:00:00+00:00',r['grace_until'])
    def test_actual_category_removed_only_after_grace(self):
        self.s.config.live=True;self.receive(event(next_order_date=CLOCK.isoformat()));api=FakeAPI();sync=Reconciler(self.s,api)
        sync.sync(CLOCK);self.assertEqual({42},api.groups[10]);sync.sync(CLOCK+dt.timedelta(days=4));self.assertEqual({42},api.groups[10]);sync.sync(CLOCK+dt.timedelta(days=5));self.assertEqual(set(),api.groups[10])
    def test_dashboard_real_evidence(self):
        self.receive();api=FakeAPI();sync=Reconciler(self.s,api);a=RenewalAuditor(self.s,api)
        data=snapshot(self.s,sync,a,CLOCK);r=data['members'][0]
        self.assertEqual('active',r['benefit_state']);self.assertIsNone(r['category_present']);self.assertIn('authenticated_subscription_event',r['evidence'][0]['detail']);self.assertEqual(20,r['promotion_id']);self.assertIn('Subscription #1001',render(data))
    def test_dashboard_empty_does_not_seed_fake_members(self):
        a=RenewalAuditor(self.s,FakeAPI());data=snapshot(self.s,Reconciler(self.s,FakeAPI()),a,CLOCK)
        self.assertEqual([],data['members']);self.assertIn('No subscriptions received yet',render(data))
    def test_grace_config(self):
        c=configuration();self.assertEqual(5,Config({'store':'example','plans':c.plans}).grace_days)
        for value in (-1,31,True,'5'):
            with self.assertRaises(ValueError):Config({'store':'example','plans':c.plans,'grace_days':value})

    def test_cancel_after_pending_keeps_paid_period(self):
        self.receive();self.receive(event(status='pending',next_order_date=None,updated_at='2026-11-16T11:15:00Z'))
        self.receive(event(status='canceled',next_order_date=None,updated_at='2026-11-16T11:30:00Z'))
        self.assertTrue(self.s.active(CLOCK));self.assertFalse(self.s.active(timestamp('2026-12-16T00:00:00Z')))
    def test_newer_undated_active_does_not_erase_lease(self):
        self.receive();self.receive(event(next_order_date=None,updated_at='2026-11-16T11:30:00Z'));self.assertTrue(self.s.active(CLOCK))

class LocalReadAccess(unittest.TestCase):
    def test_local_only(self):
        from memberships import local_read_allowed as check
        self.assertTrue(check('127.0.0.1',{'Host':'127.0.0.1:4248'},4248))
        self.assertTrue(check('127.0.0.1',{'Host':'localhost:4248','Origin':'http://localhost:4248','Sec-Fetch-Site':'same-origin'},4248))
        for client,headers in [('1.2.3.4',{'Host':'localhost:4248'}),('127.0.0.1',{'Host':'evil.test:4248'}),('127.0.0.1',{'Host':'localhost:4248','Origin':'https://evil.test'}),('127.0.0.1',{'Host':'localhost:4248','X-Forwarded-For':'1.2.3.4'}),('127.0.0.1',{'Host':'localhost:4248','Sec-Fetch-Site':'cross-site'})]:
            self.assertFalse(check(client,headers,4248))
