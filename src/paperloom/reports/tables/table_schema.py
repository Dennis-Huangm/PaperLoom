"""Column ownership shared by ingestion, evidence review and publication.

Reference columns carry locators, never experimental values. Only explicit
reference labels qualify; words such as 'evidence score' remain data columns.
"""
import re
import unicodedata


def column_role(header: str) -> str:
    value = unicodedata.normalize('NFKC', header).strip(' *`').casefold()
    value = re.sub(r'\s+', '', value)
    return 'reference' if re.fullmatch(
        r'(?:依据|原文依据|支持依据)(?:\((?:摘录|证据id|来源|引用)\))?', value
    ) else 'data'


def reference_label(header: str) -> str:
    """Canonical label for legacy adapters; typed catalogues keep raw headers."""
    return '原文依据' if column_role(header) == 'reference' else header
