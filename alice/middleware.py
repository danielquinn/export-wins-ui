import jwt

from django.conf import settings

from .models import User


class AliceMiddleware(object):

    def process_request(self, request):

        alice = request.COOKIES.get("alice")

        request.alice_id = None
        request.user = User(is_authenticated=False)

        try:
            token = jwt.decode(alice, settings.UI_SECRET)
            request.alice_id = token["session"]
            request.user = User(**token["user"])
        except:
            pass  # Any failure here means we deny login status
