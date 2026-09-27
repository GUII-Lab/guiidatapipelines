"""Cookie/CSRF browser fixture; raw Client remains in CSRF rejection tests."""
from django.test import Client


class SessionClient(Client):
    def __init__(self, **kwargs):
        super().__init__(enforce_csrf_checks=True, **kwargs)

    def generic(self, method, path, *args, **kwargs):
        if method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            response = self.get("/datapipeline/api/v1/instructor_csrf/")
            if response.status_code == 200:
                kwargs.setdefault("HTTP_X_CSRFTOKEN", response.json()["csrf_token"])
        return super().generic(method, path, *args, **kwargs)
