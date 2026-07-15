from django.http import JsonResponse
from orders.models import Order


def orders_summary(request):
    """
    Dashboard summary endpoint. For each order, computes total item count
    and total item revenue. Originally fast — became slow after OrderItem
    was introduced, because this loop touches order.items for every order.
    """
    orders = Order.objects.all()  # scoped by TenantManager

    data = []
    for order in orders:
        item_count = order.items.count()
        item_total = sum(item.price * item.quantity for item in order.items.all())
        data.append({
            'id': order.id,
            'customer_name': order.customer_name,
            'status': order.status,
            'item_count': item_count,
            'item_total': str(item_total),
        })

    return JsonResponse({'orders': data})
