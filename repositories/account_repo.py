from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountCatalog


class AccountRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, account_id: str) -> AccountCatalog | None:
        return self.session.get(AccountCatalog, account_id)

    def get_by_code(self, code: str) -> AccountCatalog | None:
        stmt = select(AccountCatalog).where(AccountCatalog.code == str(code or "").strip(), AccountCatalog.active == 1)
        return self.session.scalar(stmt)

    def get_by_name(self, name: str) -> AccountCatalog | None:
        needle = str(name or "").strip().lower()
        if not needle:
            return None
        stmt = select(AccountCatalog).where(AccountCatalog.active == 1)
        for account in self.session.scalars(stmt):
            if account.name.strip().lower() == needle:
                return account
        return None

    def list_active(self) -> list[AccountCatalog]:
        stmt = select(AccountCatalog).where(AccountCatalog.active == 1).order_by(AccountCatalog.section, AccountCatalog.name)
        return list(self.session.scalars(stmt))

    def create(self, **data) -> AccountCatalog:
        account = AccountCatalog(**data)
        self.session.add(account)
        self.session.flush()
        return account
