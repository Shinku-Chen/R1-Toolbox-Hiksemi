"""Bounded, credential-free evidence for small-screen fill requests.

The screen and the resident Web share this process.  Keep only short-lived
origin/auth/outcome classes so a missing fill can be distinguished from a
request rejected by an existing gate.  Never retain token values, headers,
bodies, paths, or peer addresses.
"""
import collections
import threading
import time

_LIMIT = 8
_lock = threading.Lock()
_events = collections.deque(maxlen=_LIMIT)
_sequence = 0


def _origin_class(origin):
    if origin is None:
        return 'none'
    if origin == 'null':
        return 'null'
    if origin.startswith('http://127.0.0.1:') or origin.startswith('http://localhost:'):
        return 'loopback'
    return 'other'


def note_request(*, kind, origin, token_present=False, token_match=False,
                 outcome='received', reason=None):
    """Append one bounded event; return its sequence id for a final update."""
    global _sequence
    with _lock:
        _sequence += 1
        sequence = _sequence
        _events.append({
            'sequence': sequence,
            'at': time.time(),
            'kind': kind,
            'origin': _origin_class(origin),
            'token_present': bool(token_present),
            'token_match': bool(token_match),
            'outcome': outcome,
            'reason': reason if isinstance(reason, str) and len(reason) <= 48 else None,
        })
        return sequence


def note_outcome(sequence, *, token_present=None, token_match=None, outcome, reason=None):
    """Update an existing event without replacing its arrival evidence."""
    with _lock:
        for event in _events:
            if event.get('sequence') == sequence:
                if token_present is not None:
                    event['token_present'] = bool(token_present)
                if token_match is not None:
                    event['token_match'] = bool(token_match)
                event['outcome'] = outcome
                event['reason'] = reason if isinstance(reason, str) and len(reason) <= 48 else None
                return


def snapshot():
    with _lock:
        return {'schema_version': 1, 'limit': _LIMIT,
                'events': [dict(event) for event in _events]}
