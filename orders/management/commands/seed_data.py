from django.core.management.base import BaseCommand
from tenants.models import Tenant
from orders.models import Order
import random


class Command(BaseCommand):
    help = "Seed the database with sample tenants and orders for testing"

    def handle(self, *args, **options):
        Order.objects.all().delete()
        Tenant.objects.all().delete()

        tenant_a = Tenant.objects.create(name="Acme Corp", slug="acme")
        tenant_b = Tenant.objects.create(name="Globex Inc", slug="globex")

        for i in range(250):
            Order.objects.create(
                tenant=tenant_a,
                customer_name=f"Customer {i}",
                total_amount=random.uniform(10, 500),
                status=random.choice(['pending', 'completed', 'cancelled'])
            )

        for i in range(5):
            Order.objects.create(
                tenant=tenant_b,
                customer_name=f"Customer {i}",
                total_amount=random.uniform(10, 500),
                status='completed'
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {tenant_a.name} with 250 orders, {tenant_b.name} with 5 orders"
        ))
