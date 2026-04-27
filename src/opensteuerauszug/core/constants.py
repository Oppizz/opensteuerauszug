from decimal import Decimal
from typing import Set

# Legacy quantity sentinel retained for backward compatibility with older test
# fixtures and importer data paths. New code should use None for missing
# quantity values.
UNINITIALIZED_QUANTITY = Decimal("-1")
# Standard Swiss withholding tax rate used for revenue subject to withholding.
WITHHOLDING_TAX_RATE = Decimal("0.35")

# Signs that indicate non-taxable payments that should be skipped entirely
NON_TAXABLE_SIGNS: Set[str] = {
    "KEP",  # Return of capital contributions
    "(KG)",  # Capital gain
    "(KR)",  # Return of Capital
}