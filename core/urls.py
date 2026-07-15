"""
URL configuration for core project.
"""
from django.contrib import admin
from django.urls import path, include
from orders.views import orders_summary

urlpatterns = [
    path('admin/', admin.site.urls),
    path('silk/', include('silk.urls', namespace='silk')),
    path('api/orders/summary/', orders_summary, name='orders-summary'),
]
