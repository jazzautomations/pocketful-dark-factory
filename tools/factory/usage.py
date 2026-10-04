"""Read timing evidence from an unchanged BAND export; never invent usage."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


def field(message, camel, snake):
    if camel in message and snake in message and message[camel] != message[snake]:
        raise ValueError('conflicting export field aliases')
    value = message.get(camel, message.get(snake))
    if not isinstance(value, str) or not value:
        raise ValueError('missing or invalid export field: '+camel)
    return value


def audit(raw):
    document = json.loads(raw.decode('utf-8-sig'))
    messages = document.get('messages') if isinstance(document, dict) else None
    if not isinstance(messages, list) or not messages:
        raise ValueError('export must contain a nonempty messages array')
    timed = []
    usage_events = 0
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('invalid message record')
        stamp = field(message, 'insertedAt', 'inserted_at')
        instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError('every timestamp must include a timezone')
        sender = field(message, 'senderType', 'sender_type')
        metadata = message.get('metadata') or {}
        if not isinstance(metadata, dict):
            raise ValueError('invalid message metadata')
        usage_events += int('band_usage' in metadata)
        timed.append((instant.astimezone(timezone.utc), sender))
    timed.sort(key=lambda item: item[0])
    humans = [t for t,s in timed if s.lower() == 'user']
    if not humans:
        raise ValueError('no human dispatch recorded; autonomy cannot be established')
    dispatch = humans[0]
    return {
        'schema_version': 'band-export-audit.v1',
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'message_count': len(messages),
        'human_message_count': len(humans),
        'human_messages_after_dispatch': len(humans)-1,
        'dispatch_at': dispatch.isoformat(),
        'last_event_at': timed[-1][0].isoformat(),
        'dispatch_to_last_event_seconds': (timed[-1][0]-dispatch).total_seconds(),
        'room_interval_seconds': (timed[-1][0]-timed[0][0]).total_seconds(),
        'duration_scope': 'first human message to last exported event; not independently identified final report',
        'usage': {
            'status': 'unavailable',
            'reason': 'counter_semantics_not_verified' if usage_events else 'no_usage_events_in_export',
            'recorded_event_count': usage_events,
        },
    }


def main():
    if len(sys.argv) != 2:
        print('Usage: python usage.py room.json', file=sys.stderr)
        return 2
    try:
        result = audit(Path(sys.argv[1]).read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        print('Export audit failed: '+str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
