import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

def local_time(epoch):
    name = os.environ.get('TZ', 'UTC').strip() or 'UTC'
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return datetime.fromtimestamp(epoch, zone).strftime('%Y-%m-%d %H:%M %Z')
