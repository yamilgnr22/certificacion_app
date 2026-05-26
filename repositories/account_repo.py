from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from accounting_accounts import plain_account_text
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

    def find_by_text(self, value: str) -> AccountCatalog | None:
        needle = plain_account_text(value)
        if not needle:
            return None
        stmt = select(AccountCatalog).where(AccountCatalog.active == 1)
        for account in self.session.scalars(stmt):
            candidates = [
                account.code,
                account.niif_code,
                account.name,
            ]
            try:
                aliases = json.loads(account.aliases_json or "[]")
                if isinstance(aliases, list):
                    candidates.extend(str(alias) for alias in aliases)
            except Exception:
                pass
            if any(plain_account_text(candidate) == needle for candidate in candidates if candidate):
                return account
        return None

    def list_active(self) -> list[AccountCatalog]:
        stmt = (
            select(AccountCatalog)
            .where(AccountCatalog.active == 1)
            .order_by(AccountCatalog.display_order, AccountCatalog.section, AccountCatalog.name)
        )
        return list(self.session.scalars(stmt))

    def list_filtered(self, *, query: str = "", account_type: str = "", section: str = "") -> list[AccountCatalog]:
        stmt = select(AccountCatalog).where(AccountCatalog.active == 1)
        if account_type:
            stmt = stmt.where(AccountCatalog.account_type == account_type)
        if section:
            stmt = stmt.where(AccountCatalog.section == section)
        records = list(self.session.scalars(stmt.order_by(AccountCatalog.display_order, AccountCatalog.name)))
        needle = plain_account_text(query)
        if not needle:
            return records
        filtered: list[AccountCatalog] = []
        for account in records:
            haystack = " ".join(
                str(part or "")
                for part in [account.code, account.niif_code, account.name, account.aliases_json]
            )
            if needle in plain_account_text(haystack):
                filtered.append(account)
        return filtered

    def create(self, **data) -> AccountCatalog:
        account = AccountCatalog(**data)
        self.session.add(account)
        self.session.flush()
        return account
