import pytest
from django.test import TestCase
from tenants.models import Tenant
from tenants.context import set_current_tenant, clear_current_tenant
from orders.models import Order


class TenantIsolationTests(TestCase):
    """
    Proves the negative: tenant scoping cannot be bypassed, accidentally
    or otherwise, through the standard .objects manager.
    """

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme Corp", slug="acme")
        self.tenant_b = Tenant.objects.create(name="Globex Inc", slug="globex")

        self.order_a = Order.all_objects.create(
            tenant=self.tenant_a, customer_name="Alice", total_amount=100, status="completed"
        )
        self.order_b = Order.all_objects.create(
            tenant=self.tenant_b, customer_name="Bob", total_amount=200, status="completed"
        )
        
    def tearDown(self):
        clear_current_tenant()

    def test_tenant_a_sees_only_own_orders(self):
        set_current_tenant(self.tenant_a)
        orders = Order.objects.all()
        self.assertEqual(orders.count(), 1)
        self.assertEqual(orders.first().customer_name, "Alice")

    def test_tenant_b_cannot_see_tenant_a_order_by_id(self):
        """
        The critical negative test: even a direct, explicit lookup by primary
        key must not leak cross-tenant data. This is the case a developer
        is most likely to write without thinking about tenant scoping.
        """
        set_current_tenant(self.tenant_b)
        result = Order.objects.filter(id=self.order_a.id).first()
        self.assertIsNone(result)

    def test_objects_all_does_not_bypass_scoping(self):
        """
        Directly targets the brief's requirement: calling .objects.all()
        must never return unscoped data, even though nothing about the
        call site suggests filtering is happening.
        """
        set_current_tenant(self.tenant_a)
        all_orders = Order.objects.all()
        ids_returned = set(all_orders.values_list('id', flat=True))
        self.assertNotIn(self.order_b.id, ids_returned)

    def test_no_tenant_context_returns_empty_not_everything(self):
        """
        Fail-closed behaviour: if middleware never ran (e.g. a bug, a
        management command, a bare shell session), scoping must default
        to zero rows, not all rows across every tenant.
        """
        clear_current_tenant()
        orders = Order.objects.all()
        self.assertEqual(orders.count(), 0)

    def test_all_objects_manager_is_explicit_escape_hatch(self):
        """
        all_objects intentionally bypasses scoping — this is documented,
        opt-in behaviour, not a bug. Confirms it still exists and works
        for legitimate cross-tenant needs (admin, migrations, scripts).
        """
        set_current_tenant(self.tenant_a)
        total_across_all_tenants = Order.all_objects.count()
        self.assertEqual(total_across_all_tenants, 2)

    def test_switching_tenant_context_switches_visible_data(self):
        """
        Proves the manager reads live thread-local state per query,
        not a cached value from when the queryset was first built.
        """
        set_current_tenant(self.tenant_a)
        self.assertEqual(Order.objects.count(), 1)

        set_current_tenant(self.tenant_b)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.first().customer_name, "Bob")