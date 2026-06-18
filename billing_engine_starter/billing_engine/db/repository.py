"""
Repositories — the ONLY place SQL lives.

Each repository wraps the Database connection and exposes methods that
take/return domain dataclasses (defined in billing_engine/models/).

⚠️ YOU IMPLEMENT every method body marked TODO.
   The signatures, docstrings, and the LedgerRepository's append-only
   guarantee are already in place — do not change them.

Beginner map (Day 2):
  1) CustomerRepository: add, get, find_by_email, list_all
  2) PlanRepository: add, get, list_all
  3) PlanTierRepository: add, list_for_plan
  4) DiscountRepository: add, get_by_code
  5) SubscriptionRepository: add, get, list_all, get_due_for_billing
  6) UsageRecordRepository: add, sum_for_period
  7) InvoiceRepository: add, get
  8) InvoiceLineItemRepository: add, list_for_invoice

Skip on Day 2 (read-only for now):
  - SubscriptionRepository.update_period / update_status / update_plan
  - InvoiceRepository.count_for_subscription / mark_paid / mark_failed / set_pdf_path
  - LedgerRepository and PaymentAttemptRepository

Conventions:
  - Always use parameterized queries (`?` placeholders) — NEVER f-string SQL.
  - Money values are persisted as TEXT using `money.to_storage()`.
  - Dates are persisted as ISO strings (`date.isoformat()`).

New layering (beginner-friendly):
  - Raw SQL lives in `billing_engine/db/queries.py`.
  - Repository methods call those query helpers.
  - Your Day 2 focus is:
      1) Convert domain -> storage values before helper call
      2) Convert rows -> domain dataclasses after helper call
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from billing_engine.db.database import Database
from billing_engine.db import queries as q
from billing_engine.money import Money
from billing_engine.models import (
    Customer,
    Plan, PricingType, BillingPeriod,
    Subscription, SubscriptionStatus,
    Invoice, InvoiceStatus, InvoiceLineItem, LineItemKind,
    LedgerEntry, LedgerDirection,
)


# ============================================================
# CUSTOMERS
# ============================================================
# Day 2: start here.  
class CustomerRepository:
    """Persistence boundary for customers.

        A Customer is the billing account owner: invoices, subscriptions, and
        ledger entries ultimately belong to a customer. This repository hides the
        `customers` table and returns Customer dataclasses so the rest of the app
        does not need to know SQL column names. 
    """ 
        
        
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, customer: Customer) -> Customer:
        with self.db.transaction() as conn:
            customer_id = q.insert_customer(
                conn,
                customer.name,
                customer.email,
                customer.country_code,
                customer.state_code,
            )

        return Customer(
            id=customer_id,
            name=customer.name,
            email=customer.email,
            country_code=customer.country_code,
            state_code=customer.state_code,
        )

    def get(self, customer_id: int) -> Optional[Customer]:
        with self.db.connect() as conn:
            row = q.select_customer_by_id(conn, customer_id)

        if row is None:
            return None

        return Customer(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            country_code=row["country_code"],
            state_code=row["state_code"],
        )

    def find_by_email(self, email: str) -> Optional[Customer]:
        with self.db.connect() as conn:
            row = q.select_customer_by_email(conn, email)

        if row is None:
            return None

        return Customer(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            country_code=row["country_code"],
            state_code=row["state_code"],
        )

    def list_all(self) -> list[Customer]:
        with self.db.connect() as conn:
            rows = q.select_all_customers(conn)

        return [
            Customer(
                id=r["id"],
                name=r["name"],
                email=r["email"],
                country_code=r["country_code"],
                state_code=r["state_code"],
            )
            for r in rows
        ]
        
# ============================================================
# PLANS  +  PLAN TIERS
# ============================================================
# Day 2
class PlanRepository:
    """Persistence boundary for subscription plans.

    A Plan describes what the customer bought: pricing type, billing period,
    currency, and strategy configuration. Pricing code consumes Plan objects,
    while this repository handles the `plans` table representation.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, plan: Plan) -> Plan:
        with self.db.transaction() as conn:
            plan_id = q.insert_plan(
                conn,
                plan.name,
                plan.pricing_type.value,
                plan.billing_period.value,
                plan.currency,
                plan.config_json,
            )

        return Plan(
            id=plan_id,
            name=plan.name,
            pricing_type=plan.pricing_type,
            billing_period=plan.billing_period,
            currency=plan.currency,
            config_json=plan.config_json,
        )
        
    def get(self, plan_id: int) -> Optional[Plan]:
        with self.db.connect() as conn:
            row = q.select_plan_by_id(conn, plan_id)

        if row is None:
            return None

        return Plan(
            id=row["id"],
            name=row["name"],
            pricing_type=PricingType(row["pricing_type"]),
            billing_period=BillingPeriod(row["billing_period"]),
            currency=row["currency"],
            config_json=row["config_json"],
        )
        
    def list_all(self) -> list[Plan]:
        with self.db.connect() as conn:
            rows = q.select_all_plans(conn)

        return [
            Plan(
                id=r["id"],
                name=r["name"],
                pricing_type=PricingType(r["pricing_type"]),
                billing_period=BillingPeriod(r["billing_period"]),
                currency=r["currency"],
                config_json=r["config_json"],
            )
            for r in rows
        ]


class PlanTierRepository:
    """Persistence boundary for pricing tiers attached to a plan.

    Tiered and usage-based plans need rows such as "0-100 units at 1.00" and
    "101+ units at 0.75". These rows live separately from plans because one
    plan can have many tiers.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, plan_id: int, from_units: int, to_units: Optional[int], unit_price: Money) -> int:
        with self.db.transaction() as conn:
            tier_id = q.insert_plan_tier(
                conn,
                plan_id,
                from_units,
                to_units,
                unit_price.to_storage(),
            )

        return tier_id
    
    def list_for_plan(self, plan_id: int, currency: str) -> list[tuple[int, Optional[int], Money]]:
        with self.db.connect() as conn:
            rows = q.select_plan_tiers(conn, plan_id)

        return [
            (
                r["from_units"],
                r["to_units"],
                Money(r["unit_price"], currency),
            )
            for r in rows
        ]

# ============================================================
# DISCOUNTS
# ============================================================
# Day 2
class DiscountRepository:
    """Persistence boundary for discount definitions.

    Discounts are stored as flexible rows because different discount types need
    different interpretation: percentage, fixed amount, or first-month-free.
    This repository intentionally returns dictionaries instead of a dataclass.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, code: str, discount_type: str, value: str, currency: Optional[str] = None) -> int:
        with self.db.transaction() as conn:
            discount_id = q.insert_discount(
                conn,
                code,
                discount_type,
                value,
                currency,
            )
        return discount_id

    def get_by_code(self, code: str) -> Optional[dict]:
        with self.db.connect() as conn:
            row = q.select_discount_by_code(conn, code)

        return dict(row) if row else None

# ============================================================
# SUBSCRIPTIONS
# ============================================================
# Day 2 (only add/get/list_all/get_due_for_billing)
class SubscriptionRepository:
    """Persistence boundary for customer subscriptions.

    A Subscription connects a customer to a plan and tracks lifecycle state:
    TRIAL, ACTIVE, PAST_DUE, or CANCELLED. It also stores the current billing
    period, trial end date, optional discount, and dunning state.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, subscription: Subscription) -> Subscription:
        with self.db.transaction() as conn:
            sub_id = q.insert_subscription(
                conn,
                subscription.customer_id,
                subscription.plan_id,
                subscription.status.value,
                subscription.current_period_start.isoformat(),
                subscription.current_period_end.isoformat(),
                subscription.trial_end.isoformat() if subscription.trial_end else None,
                subscription.discount_id,
                subscription.past_due_since.isoformat() if subscription.past_due_since else None,
            )

        return Subscription(
            id=sub_id,
            customer_id=subscription.customer_id,
            plan_id=subscription.plan_id,
            status=subscription.status,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            trial_end=subscription.trial_end,
            discount_id=subscription.discount_id,
            past_due_since=subscription.past_due_since,
        )

    def get(self, subscription_id: int) -> Optional[Subscription]:
        with self.db.connect() as conn:
            row = q.select_subscription_by_id(conn, subscription_id)

        if row is None:
            return None

        return Subscription(
            id=row["id"],
            customer_id=row["customer_id"],
            plan_id=row["plan_id"],
            status=SubscriptionStatus(row["status"]),
            current_period_start=date.fromisoformat(row["current_period_start"]),
            current_period_end=date.fromisoformat(row["current_period_end"]),
            trial_end=date.fromisoformat(row["trial_end"]) if row["trial_end"] else None,
            discount_id=row["discount_id"],
            past_due_since=date.fromisoformat(row["past_due_since"]) if row["past_due_since"] else None,
        )
    
    def list_all(self) -> list[Subscription]:
        with self.db.connect() as conn:
            rows = q.select_all_subscriptions(conn)

        return [
            Subscription(
                id=r["id"],
                customer_id=r["customer_id"],
                plan_id=r["plan_id"],
                status=SubscriptionStatus(r["status"]),
                current_period_start=date.fromisoformat(r["current_period_start"]),
                current_period_end=date.fromisoformat(r["current_period_end"]),
                trial_end=date.fromisoformat(r["trial_end"]) if r["trial_end"] else None,
                discount_id=r["discount_id"],
                past_due_since=date.fromisoformat(r["past_due_since"]) if r["past_due_since"] else None,
            )
            for r in rows
        ]
    
    def get_due_for_billing(self, as_of: date) -> list[Subscription]:
        with self.db.connect() as conn:
            rows = q.select_due_subscriptions(conn, as_of.isoformat())

        return [
            Subscription(
                id=r["id"],
                customer_id=r["customer_id"],
                plan_id=r["plan_id"],
                status=SubscriptionStatus(r["status"]),
                current_period_start=date.fromisoformat(r["current_period_start"]),
                current_period_end=date.fromisoformat(r["current_period_end"]),
                trial_end=date.fromisoformat(r["trial_end"]) if r["trial_end"] else None,
                discount_id=r["discount_id"],
                past_due_since=date.fromisoformat(r["past_due_since"]) if r["past_due_since"] else None,
            )
            for r in rows
        ]
    
    # ------------------------------------------------------------------
    # Day 2 boundary:
    # Everything below this line in this class is intentionally deferred.
    # Keep the method stubs so Day 3/4 can build on the same API surface.
    # ------------------------------------------------------------------
    def update_status(
        self,
        subscription_id: int,
        new_status: SubscriptionStatus,
        past_due_since: Optional[date] = None,
    ) -> None:
        with self.db.transaction() as conn:
            q.update_subscription_status(
                conn,
                subscription_id,
                new_status.value if hasattr(new_status, "value") else new_status,
                past_due_since.isoformat() if past_due_since else None,
            )
            
    def update_period(self, subscription_id: int, new_start: date, new_end: date) -> None:
        with self.db.transaction() as conn:
            q.update_subscription_period(
                conn,
                subscription_id,
                new_start.isoformat(),
                new_end.isoformat(),
            )
            
    def update_plan(self, subscription_id: int, new_plan_id: int) -> None:
        with self.db.transaction() as conn:
            q.update_subscription_plan(
                conn,
                subscription_id,
                new_plan_id,
            )

# ============================================================
# USAGE
# ============================================================
# Day 2
class UsageRecordRepository:
    """Persistence boundary for metered usage.

    Usage records store quantities such as API calls, seats, messages, or GBs.
    Usage-based pricing strategies ask this repository for the total quantity
    they should charge for a subscription.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, subscription_id: int, metric: str, quantity: int) -> int:
        with self.db.transaction() as conn:
            return q.insert_usage_record(
                conn,
                subscription_id,
                metric,
                quantity,
            )
        
    def sum_for_period(
    self, subscription_id: int, metric: str, period_start: date, period_end: date
) -> int:
        with self.db.connect() as conn:
            result = q.sum_usage_for_subscription_metric(
                conn,
                subscription_id,
                metric,
            )

        return result or 0

# ============================================================
# INVOICES + LINE ITEMS
# ============================================================
# Day 2 (InvoiceRepository only add/get)
class InvoiceRepository:
    """Persistence boundary for invoice headers.

    An Invoice stores the totals for one subscription period: subtotal,
    discounts, tax, final total, status, issue time, and optional PDF path.
    Line items are stored separately by InvoiceLineItemRepository.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, invoice: Invoice) -> Invoice:
        with self.db.transaction() as conn:
            invoice_id = q.insert_invoice(
                conn,
                invoice.subscription_id,
                invoice.period_start.isoformat(),
                invoice.period_end.isoformat(),
                invoice.total.currency,   
                invoice.subtotal.to_storage(),
                invoice.discount_total.to_storage(),
                invoice.tax_total.to_storage(),
                invoice.total.to_storage(),
                invoice.status.value,
                invoice.issued_at.isoformat() if invoice.issued_at else None,
                invoice.pdf_path,
            )

        return Invoice(
            id=invoice_id,
            subscription_id=invoice.subscription_id,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            subtotal=invoice.subtotal,
            discount_total=invoice.discount_total,
            tax_total=invoice.tax_total,
            total=invoice.total,
            status=invoice.status,
            issued_at=invoice.issued_at,
            pdf_path=invoice.pdf_path,
        )
    
    def get(self, invoice_id: int) -> Optional[Invoice]:
        with self.db.connect() as conn:
            row = q.select_invoice_by_id(conn, invoice_id)

        if row is None:
            return None

        return Invoice(
            id=row["id"],
            subscription_id=row["subscription_id"],
            period_start=date.fromisoformat(row["period_start"]),
            period_end=date.fromisoformat(row["period_end"]),
            subtotal=Money(row["subtotal"], row["currency"]),
            discount_total=Money(row["discount_total"], row["currency"]),
            tax_total=Money(row["tax_total"], row["currency"]),
            total=Money(row["total"], row["currency"]),
            status=InvoiceStatus(row["status"]),
            issued_at=date.fromisoformat(row["issued_at"]) if row["issued_at"] else None,
            pdf_path=row["pdf_path"],
        )

    def count_for_subscription(self, subscription_id: int) -> int:
        with self.db.connect() as conn:
            return q.count_invoices_for_subscription(conn, subscription_id)
        
    def mark_paid(self, invoice_id: int) -> None:
        with self.db.transaction() as conn:
            q.update_invoice_status(
                conn,
                invoice_id,
                "PAID",
            )
        
    def mark_failed(self, invoice_id: int) -> None:
         with self.db.transaction() as conn:
            q.update_invoice_status(
                conn,
                invoice_id,
                "FAILED",
            )
        
    def set_pdf_path(self, invoice_id: int, path: str) -> None:
        # TODO Day 4.
        # Hint: q.update_invoice_pdf_path(...)
        raise NotImplementedError("Day 4: implement InvoiceRepository.set_pdf_path")


class InvoiceLineItemRepository:
    """Persistence boundary for invoice detail rows.

    Line items explain how the invoice total was built: base charge, usage,
    discount, tax, or proration. They are separate from the invoice header so
    one invoice can contain multiple visible charges and credits.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, line_item: InvoiceLineItem) -> InvoiceLineItem:
        with self.db.transaction() as conn:
            line_item_id = q.insert_invoice_line_item(
                conn,
                line_item.invoice_id,
                line_item.description,
                line_item.amount.to_storage(),
                line_item.kind.value,
            )

        return InvoiceLineItem(
            id=line_item_id,
            invoice_id=line_item.invoice_id,
            description=line_item.description,
            amount=line_item.amount,
            kind=line_item.kind,
        )
    
    def list_for_invoice(self, invoice_id: int) -> list[InvoiceLineItem]:
        with self.db.connect() as conn:
            rows = q.select_line_items_for_invoice(conn, invoice_id)

        return [
            InvoiceLineItem(
                id=r["id"],
                invoice_id=r["invoice_id"],
                description=r["description"],
                amount=Money(r["amount"], "USD"),  
                kind=LineItemKind(r["kind"]),
            )
            for r in rows
        ]

# ============================================================
# DAY 3/4 ONLY — keep stubs for later
# ============================================================

# ============================================================
# LEDGER — APPEND-ONLY (do not implement update/delete)
# ============================================================
class LedgerRepository:
    """Persistence boundary for the append-only accounting ledger.

    The ledger records financial movements: DEBIT when the customer owes money,
    CREDIT when money is received or reversed. It is append-only so history is
    auditable; mistakes should be corrected with reversing entries, not edits.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, entry: LedgerEntry) -> LedgerEntry:
        with self.db.transaction() as conn:
            entry_id = q.insert_ledger_entry(
                conn,
                entry.customer_id,
                entry.invoice_id,
                entry.amount.to_storage(),
                entry.amount.currency,
                entry.direction.value,
                entry.reason,  
            )

        return LedgerEntry(
            id=entry_id,
            invoice_id=entry.invoice_id,
            customer_id=entry.customer_id,
            amount=entry.amount,
            direction=entry.direction,
            reason=entry.reason,
            created_at=None,
        )
        
    def list_for_customer(self, customer_id: int) -> list[LedgerEntry]:
        with self.db.connect() as conn:
            rows = q.select_ledger_for_customer(conn, customer_id)

        return [
            LedgerEntry(
                id=r["id"],
                invoice_id=r["invoice_id"],
                customer_id=r["customer_id"],
                amount=Money(r["amount"], r["currency"]),
                direction=LedgerDirection(r["direction"]),
                reason=r["reason"],
                created_at=None,
            )
            for r in rows
        ]
    # These two methods are intentionally implemented to REJECT — do not override.
    def update(self, *args, **kwargs):
        raise NotImplementedError("Ledger is append-only. Post a reversing entry instead.")

    def delete(self, *args, **kwargs):
        raise NotImplementedError("Ledger is append-only. Post a reversing entry instead.")


# ============================================================
# PAYMENT ATTEMPTS
# ============================================================
class PaymentAttemptRepository:
    """Persistence boundary for payment retry history.

    Each payment attempt records whether charging an invoice succeeded or
    failed, why it failed, and when the next retry should happen. This history
    powers the Day 3/4 dunning flow.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        invoice_id: int,
        attempt_no: int,
        status: str,
        failure_reason: Optional[str],
        next_retry_at: Optional[datetime],
    ) -> int:
        with self.db.transaction() as conn:
            cur = q.insert_payment_attempt(
                conn,
                invoice_id,
                attempt_no,
                status,
                failure_reason,
                next_retry_at.isoformat() if next_retry_at else None,
            )
            return cur
    
    def list_for_invoice(self, invoice_id: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = q.select_attempts_for_invoice(conn, invoice_id)

        return [dict(r) for r in rows]

    def count_for_invoice(self, invoice_id: int) -> int:
        with self.db.connect() as conn:
            result = q.count_attempts_for_invoice(conn, invoice_id)

        return result or 0