import os
import django
import asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aamyproject.settings")
django.setup()

from asgiref.testing import ApplicationCommunicator
from aamyproject.asgi import application

async def test_ws():
    scope = {
        "type": "websocket",
        "path": "/ws/groups/1/",
        "raw_path": b"/ws/groups/1/",
        "query_string": b"token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzgyMzc4NDYyLCJpYXQiOjE3ODIyOTIwNjIsImp0aSI6IjY4N2ZiY2QwZWM4YjQ2NDY4OWVmNGI0YmVkNjVhNWI0IiwidXNlcl9pZCI6IjMifQ.xRYEWta_jGbzFPOIN7TJFsFwQ6vrzQkkJk_LAq9hcoM",
        "headers": [],
        "subprotocols": [],
    }
    communicator = ApplicationCommunicator(application, scope)
    await communicator.send_input({"type": "websocket.connect"})
    
    try:
        response = await communicator.receive_output(timeout=2)
        print("Response:", response)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_ws())
