from contextvars import ContextVar


_tenant_db_alias: ContextVar[str] = ContextVar("tenant_db_alias", default="default")


def get_current_tenant_db() -> str:
    return _tenant_db_alias.get()


def set_current_tenant_db(db_alias: str):
    return _tenant_db_alias.set(db_alias or "default")


def reset_current_tenant_db(token) -> None:
    _tenant_db_alias.reset(token)

