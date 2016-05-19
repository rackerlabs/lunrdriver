import unittest

from six import string_types
from six.moves import builtins
setattr(builtins, '_', lambda x: x)

from six.moves import StringIO
from cgi import parse_qsl
from contextlib import contextmanager

from lunrdriver.lunr import client


@contextmanager
def patch(obj, attr_name, mock):
    _orig = getattr(obj, attr_name)
    try:
        setattr(obj, attr_name, mock)
        yield
    finally:
        setattr(obj, attr_name, _orig)


def urldecode(qs):
    # TODO: use webob MultiDict?
    return dict(parse_qsl(qs))


class MockResponse(StringIO):

    def getcode(self):
        return 200



class ClientTestCase(unittest.TestCase):

    def setUp(self):
        self._orig_urlopen = client.urlopen
        client.urlopen = self.mock_urlopen

    def tearDown(self):
        client.urlopen = self._orig_urlopen
        del self.request_callback

    @property
    def resp(self):
        """Get test provided next resp for ulropen, and clear next resp"""
        try:
            resp = next(self._resp)
            return resp
        except AttributeError:
            return ''
        except StopIteration:
            # clean up state for next test
            del self._resp
            # blow up test consuming too many responses
            raise

    @resp.setter
    def resp(self, resp=''):
        """Set next resp from urlopen"""
        if isinstance(resp, string_types) or isinstance(resp, Exception):
            resp = [resp]
        self._resp = (r for r in resp)

    def get_wrapped_callback(self, callback):
        def wrapper(*args, **kwargs):
            wrapper.called.append(True)
            return callback(*args, **kwargs)
        wrapper.called = []
        return wrapper

    @property
    def request_callback(self):
        if not hasattr(self, '_callback'):
            self._callback = self.get_wrapped_callback(
                lambda *args, **kwargs: None)
        return self._callback

    @request_callback.setter
    def request_callback(self, callback):
        self._callback = self.get_wrapped_callback(callback)

    @request_callback.deleter
    def request_callback(self):
        if hasattr(self, '_callback'):
            del self._callback

    def mock_urlopen(self, req):
        self.request_callback(req)
        resp = self.resp  # get next resp
        if isinstance(resp, Exception):
            # might want to construct URLError here?
            raise resp
        if not hasattr(resp, 'read'):
            resp = MockResponse(resp)
        return resp
