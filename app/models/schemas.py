from datetime import date
from typing import Optional
from pydantic import BaseModel, Field, field_validator
class ClarificationIn(BaseModel):
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    status: str = "pending"
    answer: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        allowed_statuses = {"pending", "answered", "unknown"}
        if value not in allowed_statuses:
            raise ValueError(
                "status must be pending, answered, or unknown"
            )
        return value

class LineItemIn(BaseModel):
    name: str = Field(min_length=1)
    quantity: Optional[float] = Field(default=None, gt=0)
    unit_price_minor: Optional[int] = Field(default=None, ge=0)
    total_minor: Optional[int] = Field(default=None, ge=0)
    category: Optional[str] = None

class TransactionIn(BaseModel):
    merchant: Optional[str] = None
    txn_date: Optional[date] = None
    txn_time: Optional[str] = None
    total_minor: Optional[int] = Field(default=None, ge=0)
    currency: Optional[str] = None
    tax_minor: Optional[int] = Field(default=None, ge=0)
    discount_minor: Optional[int] = Field(default=None, ge=0)
    other_charges_minor: Optional[int] = Field(default=None, ge=0)
    payment_method: Optional[str] = None
    invoice_number: Optional[str] = None
    category: Optional[str] = None
    uncertain_fields: list[str] = Field(default_factory=list)
    items: list[LineItemIn] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def currency_must_be_three_letters(cls, value):
        if value is None:
            return None

        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a 3-letter code such as INR")
        return value