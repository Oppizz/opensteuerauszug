from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class PaymentReconciliationRow(BaseModel):
    country: str
    security: str
    identifier: Optional[str] = None
    payment_date: date
    kursliste_dividend_chf: Decimal = Field(default=Decimal("0"))
    kursliste_withholding_chf: Decimal = Field(default=Decimal("0"))
    kursliste_amount_currency: Optional[str] = None
    broker_dividend_amount: Optional[Decimal] = None
    broker_dividend_currency: Optional[str] = None
    broker_withholding_amount: Optional[Decimal] = None
    broker_withholding_currency: Optional[str] = None
    broker_withholding_entry_text: Optional[str] = None
    exchange_rate: Optional[Decimal] = None
    accumulating: bool = False
    matched: bool = False
    status: str = "mismatch"
    note: Optional[str] = None
    kursliste: bool = True
    kursliste_security: bool = True
    kursliste_undefined: Optional[bool] = None

class TaxValueReconciliationRow(BaseModel):
    country: str
    security: str
    kursliste_value_chf: Decimal = Field(default=Decimal("0"))
    broker_amount: Optional[Decimal] = None
    broker_amount_currency: Optional[str] = None
    exchange_rate: Optional[Decimal] = None
    matched: bool = False
    status: str = "mismatch"
    note: Optional[str] = None
    kursliste: bool = True
    kursliste_security: bool = True
    kursliste_undefined: Optional[bool] = None    

class PaymentReconciliationReport(BaseModel):
    rows: List[PaymentReconciliationRow] = Field(default_factory=list)
    tax_value_rows: List[TaxValueReconciliationRow] = Field(default_factory=list)
    match_count: int = 0
    mismatch_count: int = 0
    expected_missing_count: int = 0
    capped_count: int = 0
