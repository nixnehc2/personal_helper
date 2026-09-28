"""Bounded SQL queries across separate source databases; no payloads decoded before LIMIT."""
from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .email_adapter import _parse_date


def normalized_time(value, source):
    """SQLite scalar function: normalize only the date, preserving microsecond ordering."""
    if source == 'email':
        value = _parse_date(value)
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, TypeError):
        return None


def page_messages(backends, *, conversation, begin, end, imported, limit, offset):
    # ATTACH queries the existing stores. The in-memory connection holds no message table.
    with closing(sqlite3.connect(':memory:')) as db:
        db.create_function('message_time', 2, normalized_time, deterministic=True)
        selects, parameters = [], []
        for number, (source, backend) in enumerate(backends.items()):
            path, projection = backend.listing_sql(f'source_{number}')
            if path is None:
                continue
            db.execute(f'ATTACH DATABASE ? AS source_{number}', (str(path),))
            conditions = []
            if conversation is not None:
                conditions.append('(? = conversation1 OR ? = conversation2 OR ? = conversation3)')
                parameters.extend([conversation] * 3)
            if imported is not None:
                conditions.append('imported = ?')
                parameters.append(imported)
            for bound, operator in ((begin, '>='), (end, '<=')):
                if bound is not None:
                    conditions.append(f'stamp {operator} ?')
                    parameters.append(bound.isoformat(timespec='microseconds'))
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            selects.append(f'SELECT source, id, payload, stamp FROM ({projection}){where}')
        if not selects:
            return []
        # Decimal IDs remain TEXT: length plus lexicographic order is exact for arbitrary size.
        # Unknown dates share datetime.min with the previous Python ordering.
        sql = ('SELECT source, payload FROM (' + ' UNION ALL '.join(selects) + ') '
               "ORDER BY COALESCE(stamp, '0001-01-01T00:00:00.000000+00:00') DESC, "
               'length(id) DESC, id DESC, source ASC LIMIT ? OFFSET ?')
        rows = db.execute(sql, [*parameters, limit, offset])
        return [backends[source].decode_listing(payload) for source, payload in rows]
