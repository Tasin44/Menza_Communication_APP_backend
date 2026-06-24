import os
import django
import asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aamyproject.settings")
django.setup()

from groupapp.models import GroupMember
from authapp.models import User
from channels.db import database_sync_to_async

async def test():
    user = await database_sync_to_async(User.objects.first)()
    print(f"User: {user}")
    
    @database_sync_to_async
    def _check_membership():
        return GroupMember.objects.filter(
            group_id=1,
            user=user,
            is_active=True,
        ).exists()

    res = await _check_membership()
    print(f"Result: {res}")

if __name__ == "__main__":
    asyncio.run(test())
