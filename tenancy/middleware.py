import time
from pathlib import Path
import os

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

from .context import reset_current_tenant_db, set_current_tenant_db
from .db import ensure_tenant_database_ready
from .models import Tenant


class TenantContextMiddleware(MiddlewareMixin):
    """
    Resolves tenant from request and stores current DB alias in request context.
    Resolution order:
    1) request host / tenant hostname
    2) X-Tenant-Code header
    3) ?tenant=<code> query param
    4) session['tenant_code']
    """

    SESSION_KEY = "tenant_code"
    HEADER_KEY = "X-Tenant-Code"

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        return (raw_host or "").split(":", 1)[0].strip().lower()

    def _tenant_from_host(self, request):
        try:
            raw_host = request.get_host()
        except Exception:
            return None

        host = self._normalize_host(raw_host)
        if not host or host in {"localhost", "127.0.0.1", "testserver"}:
            return None

        tenant = Tenant.objects.using("default").filter(hostname__iexact=host, is_active=True).first()
        if tenant:
            return tenant

        base_domain = os.getenv("TENANT_BASE_DOMAIN", "").strip().lower().lstrip(".")
        if not base_domain or host == base_domain or not host.endswith(f".{base_domain}"):
            return None

        tenant_code = host[: -(len(base_domain) + 1)]
        return Tenant.objects.using("default").filter(code=tenant_code, is_active=True).first()

    def _debug_trace(self, request, stage, tenant_code="", db_alias=""):
        if not settings.DEBUG:
            return
        if not request.path.startswith("/manufacturing/dashboard"):
            return
        try:
            trace_path = Path(settings.BASE_DIR) / "tenant_middleware_trace.log"
            session_items = {}
            try:
                session_items = dict(request.session.items())
            except Exception:
                session_items = {"_session": "unavailable"}
            line = (
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {stage} | path={request.path} "
                f"| session_key={getattr(request.session, 'session_key', None)} "
                f"| tenant_code={tenant_code or session_items.get(self.SESSION_KEY)} "
                f"| db_alias={db_alias or getattr(request, 'tenant_db_alias', None)} "
                f"| session={session_items}\n"
            )
            with open(trace_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            return

    def process_request(self, request):
        host_tenant = self._tenant_from_host(request)
        header_code = request.headers.get(self.HEADER_KEY)
        query_code = request.GET.get("tenant")
        session_code = request.session.get(self.SESSION_KEY)
        tenant_code = host_tenant.code if host_tenant else (header_code or query_code or session_code)

        request.tenant = None
        request.tenant_db_alias = "default"
        self._debug_trace(request, "process_request.start", tenant_code=tenant_code, db_alias="default")

        if not tenant_code:
            request._tenant_ctx_token = set_current_tenant_db("default")
            self._debug_trace(request, "process_request.no_tenant", tenant_code="", db_alias="default")
            return

        tenant = host_tenant or Tenant.objects.using("default").filter(code=tenant_code, is_active=True).first()
        if not tenant:
            request.session.pop(self.SESSION_KEY, None)
            request._tenant_ctx_token = set_current_tenant_db("default")
            self._debug_trace(request, "process_request.invalid_tenant", tenant_code=tenant_code, db_alias="default")
            return

        try:
            db_alias = ensure_tenant_database_ready(tenant)
        except Exception:
            request.session.pop(self.SESSION_KEY, None)
            request.tenant = None
            request.tenant_db_alias = "default"
            request._tenant_ctx_token = set_current_tenant_db("default")
            self._debug_trace(request, "process_request.tenant_bootstrap_failed", tenant_code=tenant.code, db_alias="default")
            return

        request.tenant = tenant
        request.tenant_db_alias = db_alias
        # Avoid writing session on every request.
        # Persist only when tenant came from host/header/query or value changed.
        if host_tenant or header_code or query_code:
            if session_code != tenant.code:
                request.session[self.SESSION_KEY] = tenant.code
        request._tenant_ctx_token = set_current_tenant_db(db_alias)
        self._debug_trace(request, "process_request.resolved", tenant_code=tenant.code, db_alias=db_alias)

    def process_response(self, request, response):
        self._debug_trace(
            request,
            f"process_response.{response.status_code}",
            tenant_code=getattr(getattr(request, "tenant", None), "code", ""),
            db_alias=getattr(request, "tenant_db_alias", ""),
        )
        token = getattr(request, "_tenant_ctx_token", None)
        if token is not None:
            reset_current_tenant_db(token)
        return response
