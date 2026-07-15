from django.core.management.base import BaseCommand
from tenants.models import Tenant
from orders.models import Order, OrderItem
import random


class Command(BaseCommand):
    help = "Seed the database with sample tenants and orders for testing"

    def handle(self, *args, **options):
        Order.all_objects.all().delete()
        Tenant.objects.all().delete()

        tenant_a = Tenant.objects.create(name="Acme Corp", slug="acme")
        tenant_b = Tenant.objects.create(name="Globex Inc", slug="globex")

        for i in range(250):
            order = Order.all_objects.create(
                tenant=tenant_a,
                customer_name=f"Customer {i}",
                total_amount=random.uniform(10, 500),
                status=random.choice(['pending', 'completed', 'cancelled'])
            )
            OrderItem.objects.create(order=order, product_name=f"Product {i}-A", quantity=2, price=25.00)
            OrderItem.objects.create(order=order, product_name=f"Product {i}-B", quantity=1, price=40.00)

        for i in range(5):
            order = Order.all_objects.create(
                tenant=tenant_b,
                customer_name=f"Customer {i}",
                total_amount=random.uniform(10, 500),
                status='completed'
            )
            OrderItem.objects.create(order=order, product_name=f"Product {i}-A", quantity=2, price=25.00)
            OrderItem.objects.create(order=order, product_name=f"Product {i}-B", quantity=1, price=40.00)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {tenant_a.name} with 250 orders, {tenant_b.name} with 5 orders (each with 2 items)"
        ))
