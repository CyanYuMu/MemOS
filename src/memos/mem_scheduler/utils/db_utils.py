import sys

from datetime import datetime, timezone


# Compatibility handling: Python 3.11+ supports UTC, earlier versions use timezone.utc
if sys.version_info >= (3, 11):
    from datetime import UTC

    def get_utc_now():
        """Get current UTC datetime with compatibility for different Python versions"""
        return datetime.now(UTC)
else:

    def get_utc_now():
        """Get current UTC datetime with compatibility for different Python versions"""
        return datetime.now(timezone.utc)
