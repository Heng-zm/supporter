from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import SupporterCreate, SupporterUpdate


def test_supporter_create_normalizes_fields() -> None:
    supporter = SupporterCreate(name="  Alice  ", amount="12.50", currency="usd")
    assert supporter.name == "Alice"
    assert supporter.currency == "USD"


@pytest.mark.parametrize("field", ["name", "amount", "currency", "is_visible"])
def test_supporter_update_rejects_explicit_null_for_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        SupporterUpdate.model_validate({field: None})


def test_supporter_update_allows_clearing_nullable_fields() -> None:
    update = SupporterUpdate(message=None, avatar_url=None, payment_method=None)
    assert update.model_dump(exclude_unset=True) == {
        "message": None,
        "avatar_url": None,
        "payment_method": None,
    }
