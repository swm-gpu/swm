"""Generic billing data model for provider-agnostic cost reconciliation.

Every provider maps its billing API response into these types so the CLI
can render a uniform report regardless of the underlying provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaymentRecord:
    """A charge to the user's payment method (card, crypto, wire, etc.)."""

    date: str
    amount: float
    method: str
    description: str
    receipt_url: str | None = None


@dataclass
class UsageRecord:
    """A single line-item of resource consumption."""

    period: str
    resource: str
    hours: float | None = None
    amount: float = 0.0


@dataclass
class BillingReport:
    """Complete billing snapshot returned by a provider's billing API.

    Fields that a given provider cannot supply are left as ``None`` /
    empty-list — the renderer gracefully skips them.
    """

    provider: str
    period: str

    balance: float | None = None
    current_rate_hr: float | None = None
    lifetime_spend: float | None = None
    rate_breakdown: dict[str, float] = field(default_factory=dict)

    payments: list[PaymentRecord] = field(default_factory=list)
    usage: list[UsageRecord] = field(default_factory=list)

    local_total: float = 0.0

    @property
    def payment_total(self) -> float:
        return round(sum(p.amount for p in self.payments), 2)

    @property
    def usage_total(self) -> float:
        return round(sum(u.amount for u in self.usage), 2)
