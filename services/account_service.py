from __future__ import annotations

from sqlalchemy.orm import Session

from repositories import AccountRepository
from services.serializers import account_to_dict


REQUIRED_MODEL_ACCOUNT_CODES = {
    "cash",
    "accounts_receivable",
    "inventory",
    "ppe_real_estate",
    "ppe_equipment",
    "ppe_vehicles",
    "accum_depreciation",
    "credit_cards",
    "suppliers",
    "taxes_payable",
    "accrued_expenses",
    "loans_mortgage",
    "loans_consumo",
    "loans_personal",
    "loans_pledge",
    "loans_commercial",
    "capital",
    "retained_earnings",
    "current_earnings",
    "revenue",
    "cogs",
    "operating_expenses",
    "financial_expenses",
    "depreciation_expense",
}


class AccountCatalogService:
    def __init__(self, session: Session):
        self.repo = AccountRepository(session)

    def list(
        self,
        *,
        query: str = "",
        account_type: str = "",
        section: str = "",
        recurring: bool | None = None,
        postable: bool | None = None,
    ) -> list[dict]:
        if recurring is True:
            accounts = self.repo.list_recurring_expenses()
            if query:
                needle = query.strip().lower()
                accounts = [account for account in accounts if needle in account.name.lower() or needle in account.code.lower()]
            return [account_to_dict(account) for account in accounts]
        return [
            account_to_dict(account)
            for account in self.repo.list_filtered(query=query, account_type=account_type, section=section, postable=postable)
        ]

    def summary(self) -> dict:
        accounts = self.repo.list_active()
        required = [account for account in accounts if account.required_model_account]
        codes = {account.code for account in accounts}
        return {
            "total": len(accounts),
            "required_count": len(required),
            "missing_required": sorted(REQUIRED_MODEL_ACCOUNT_CODES - codes),
            "types": sorted({account.account_type for account in accounts}),
            "sections": sorted({account.section for account in accounts}),
        }
