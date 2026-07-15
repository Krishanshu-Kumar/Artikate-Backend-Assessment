from tenants.models import Tenant
from tenants.context import set_current_tenant, clear_current_tenant


class TenantMiddleware:
    """
    Extracts tenant from subdomain (e.g. acme.example.com) and binds it
    to thread-local storage for the duration of the request.
    Falls back to an X-Tenant-Slug header for local/dev testing without subdomains.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_slug = self._extract_tenant_slug(request)
        tenant = None

        if tenant_slug:
            tenant = Tenant.objects.filter(slug=tenant_slug).first()

        set_current_tenant(tenant)
        try:
            response = self.get_response(request)
        finally:
            clear_current_tenant()

        return response

    def _extract_tenant_slug(self, request):
        header_slug = request.headers.get('X-Tenant-Slug')
        if header_slug:
            return header_slug

        host = request.get_host().split(':')[0]
        parts = host.split('.')
        if len(parts) > 2:
            return parts[0]

        return None