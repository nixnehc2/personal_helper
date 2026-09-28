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


def page_messages(backends, *, conversation, begin, end, imported, limit, offset,
                  query=None):
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
            if query is not None:
                pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                # Only projected common values and JSON leaves; never JSON keys/containers.
                fields = ['id', 'source', 'stamp',
                          "CASE imported WHEN 1 THEN 'true' WHEN 0 THEN 'false' ELSE imported END"]
                matches = [f"CAST({field} AS TEXT) LIKE ? ESCAPE '\\'" for field in fields]
                matches.append("""EXISTS (SELECT 1 FROM json_tree(payload) AS leaf
                    WHERE leaf.atom IS NOT NULL AND
                    CAST(CASE leaf.type WHEN 'true' THEN 'true' WHEN 'false' THEN 'false'
                         ELSE leaf.atom END AS TEXT) LIKE ? ESCAPE '\\')""")
                conditions.append('(' + ' OR '.join(matches) + ')')
                parameters.extend([pattern] * (len(fields) + 1))
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
            return ([], False) if query is not None else []
        # Decimal IDs remain TEXT: length plus lexicographic order is exact for arbitrary size.
        # Unknown dates share datetime.min with the previous Python ordering.
        sql = ('SELECT source, payload FROM (' + ' UNION ALL '.join(selects) + ') '
               "ORDER BY COALESCE(stamp, '0001-01-01T00:00:00.000000+00:00') DESC, "
               'length(id) DESC, id DESC, source ASC LIMIT ? OFFSET ?')
        rows = db.execute(sql, [*parameters, limit, offset])
        if query is not None:
            rows = rows.fetchall()
            return ([backends[source].decode_listing(payload) for source, payload in rows[:100]],
                    len(rows) > 100)
        return [backends[source].decode_listing(payload) for source, payload in rows]
