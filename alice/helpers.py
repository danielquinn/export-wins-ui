import requests
import logging
import time

from hashlib import sha256
from urllib.parse import urlsplit

from django import forms
from django.conf import settings


TYPES_MAP = {
    "string": forms.CharField,
    "email": forms.EmailField,
    "choice": forms.ChoiceField,
    "integer": forms.IntegerField,
    "boolean": forms.BooleanField,
    "date": forms.DateField,
    "datetime": forms.DateTimeField,
}

logger = logging.getLogger(__name__)


class RabbitException(Exception):
    pass


def get_form_field(spec):
    """ Create a Django form field from a schema

    :param spec: A subset of the result from a /*/schema API call
    :return: A form field
    """

    FieldClass = TYPES_MAP[spec["type"]]

    kwargs = {
        "label": spec.get("label"),
        "required": spec.get("required") or False,
        "help_text": spec.get("help_text"),
    }

    if "choices" in spec:
        kwargs["choices"] = [
            [c["value"], c["display_name"]] for c in spec["choices"]]
        if not kwargs["required"]:
            kwargs["choices"].insert(0, ("", ""))

    if spec["type"] == "string":
        if "max_length" in spec:
            kwargs["max_length"] = spec["max_length"]
        else:
            kwargs["widget"] = forms.widgets.Textarea

    if spec["type"] == "integer":
        kwargs["max_value"] = 2000000000

    return FieldClass(**kwargs)


class Rabbit(object):

    def get(self, url, *args, **kwargs):
        return self._request("get", url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._request("post", url, *args, **kwargs)

    def put(self, url, *args, **kwargs):
        return self._request("put", url, *args, **kwargs)

    def patch(self, url, *args, **kwargs):
        return self._request("patch", url, *args, **kwargs)

    def delete(self, url, *args, **kwargs):
        return self._request("delete", url, *args, **kwargs)

    @classmethod
    def _request(cls, method, url, request=None, keep_trying=False, *args,
                 **kwargs):
        """
        A cleaner way to allow API calls to the data server without having to
        repeat all of the boiler-plate key signing stuff.

        :param method: The HTTP method in lower case: "get", "post", etc.
        :param keep_trying: Set to True if you won't settle for ConnectionError
        :param request: A django request object
        :param args: Passed directly to requests.<method>()
        :param kwargs: Passed directly to requests.<method>()

        :return: A requests response object
        """

        prepared_request = requests.Request(
            method, url, *args, **kwargs).prepare()

        # prepared_request.headers["Content-Type"] = "application/json"
        if request and request.alice_id:
            prepared_request.headers["Cookie"] = "sessionid={}".format(
                request.alice_id
            )

        url = urlsplit(url)
        path = bytes(url.path, "utf-8")
        if url.query:
            path += bytes("?{}".format(url.query), "utf-8")
        salt = bytes(settings.UI_SECRET, "utf-8")

        body = prepared_request.body or b""
        if isinstance(body, str):
            body = bytes(body, "utf-8")

        signature = sha256(path + body + salt).hexdigest()
        prepared_request.headers["X-Signature"] = signature

        response = cls.send_request(prepared_request, keep_trying)

        if response.status_code > 299:
            logger.error("Rabbit error: {} - {}".format(
                response.status_code,
                response.content
            ))

        if response.status_code == 403:
            raise RabbitException(
                "Data server access is failing for {} requests to {} with error {}.".format(
                    method, str(path, "utf-8"), response.content,
                )
            )

        return response

    @classmethod
    def send_request(cls, prepared_request, keep_trying):
        try:
            return requests.Session().send(prepared_request)
        except requests.ConnectionError as e:
            if keep_trying:
                print("Connection error.  Retrying...")
                time.sleep(1)
                return cls.send_request(prepared_request, True)
            raise e

    def push(self, url, data, request, method='post'):
        """ POST/PUT/PATCH data to URL on data server, return json response """

        assert method in ['post', 'put', 'patch', 'delete'], 'invalid method'

        resp = getattr(self, method)(url, data=data, request=request)

        # error if do not get expected response from data server
        if ((method == 'post' and resp.status_code != 201) or
                (method == 'put' and resp.status_code != 200) or
                (method == 'patch' and resp.status_code != 200) or
                (method == 'delete' and resp.status_code != 204)):

            raise Exception("Rabbit error: {} - {}".format(
                resp.status_code,
                resp.content,
            ))

        return resp.json() if method != 'delete' else ''


rabbit = Rabbit()
