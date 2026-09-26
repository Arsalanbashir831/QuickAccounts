from django.db.models.expressions import RawSQL


def uuidv7_default() -> RawSQL:
    """Keep UUID generation on PostgreSQL 18's native UUIDv7 function."""
    return RawSQL("uuidv7()", [])


def clock_timestamp_default() -> RawSQL:
    """Use the database clock for persisted accounting timestamps."""
    return RawSQL("clock_timestamp()", [])
