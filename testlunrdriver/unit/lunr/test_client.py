# Copyright (c) 2011-2013 Rackspace US, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import unittest
from six.moves.urllib.error import URLError, HTTPError
from six.moves import StringIO
import json

from lunrdriver.lunr import client


class MockResponse(object):

    def __init__(self, data='', code=200):
        self.data = data
        self.code = code

    def getcode(self):
        return self.code

    def read(self):
        body = json.dumps(self.data)
        return StringIO(body).read()


class MockUrlOpen(object):
    """
    MockUrlOpen object, you get one empty 200 response for free.
    """

    def __init__(self):
        self._responses = [MockResponse]
        self._resp_iter = None

    @property
    def responses(self):
        return self._reponses

    @responses.setter
    def responses(self, value):
        self._responses = value
        self._resp_iter = None

    def get_next_resp(self):
        if not self._resp_iter:
            self._resp_iter = iter(self._responses)
        return next(self._resp_iter)

    def __call__(self, req, *args, **kwargs):
        resp = self.get_next_resp()
        try:
            return resp(req)
        finally:
            resp.called = True


def stub_volume(**kwargs):
    stub = {
        "account_id": "project1",
        "restore_of": None,
        "created_at": "2012-10-02 18:39:57",
        "id": "vol1",
        "last_modified": "2012-10-02 18:39:57",
        "node_id": "node1",
        "size": 100,
        "status": "ACTIVE",
        "volume_type_name": "vtype",
    }
    volume = dict(stub)
    volume.update(kwargs)
    return volume


def stub_error(req, code=None, reason='Internal Server Error'):
    if not code:
        return URLError(reason)
    resp = {
        'reason': reason,
    }
    body = json.dumps(resp)
    return HTTPError(req.get_full_url(), code, reason, {}, StringIO(body))


class TestLunrClient(unittest.TestCase):

    def setUp(self):
        super(TestLunrClient, self).setUp()
        self.urlopen = MockUrlOpen()
        self._orig_urlopen = client.urlopen
        client.urlopen = self.urlopen

    def set_response(self, *responses):
        self.urlopen.responses = responses

    def tearDown(self):
        super(TestLunrClient, self).tearDown()
        client.urlopen = self._orig_urlopen

    def test_get_volume(self):
        c = client.LunrClient({'project_id': 'fake'})
        def volume_get(req):
            self.assertEqual(req.get_method(), 'GET')
            expected_path = 'http://127.0.0.1:8080/v1.0/fake/volumes/volid?'
            self.assertEqual(req.get_full_url(), expected_path)
            return MockResponse(stub_volume(account_id='fake', id='volid'))
        self.set_response(volume_get)
        resp = c.volumes.get('volid')
        self.assertTrue(volume_get.called)
        self.assertEqual(resp.body['id'], 'volid')
        self.assertEqual(resp.body['account_id'], 'fake')

    def test_get_volume_error(self):
        c = client.LunrClient({'project_id': 'fake'})
        def url_error(req):
            raise stub_error(req, reason='connection refused')
        self.set_response(url_error)
        with self.assertRaises(client.LunrError) as manager:
            c.volumes.get('volid')
            self.assertEqual(manager.exception.code, 0)
        self.assertTrue(url_error.called)
        def http_error(req):
            raise stub_error(req, 503)
        self.set_response(http_error)
        with self.assertRaises(client.LunrError) as manager:
            c.volumes.get('volid')
            self.assertEqual(manager.exception.code, 503)
        self.assertTrue(http_error.called)

    def test_export_delete(self):
        c = client.LunrClient({'project_id': 'fake'})
        def export_delete(req):
            self.assertEqual(req.get_method(), 'DELETE')
            expected_path = 'http://127.0.0.1:8080/v1.0/fake/volumes/' + \
                    'volid/export?'
            self.assertEqual(req.get_full_url(), expected_path)
            return MockResponse()
        self.set_response(export_delete)
        resp = c.exports.delete('volid')
        self.assertTrue(export_delete.called)

    def test_export_delete_force(self):
        c = client.LunrClient({'project_id': 'fake'})
        def export_delete_force(req):
            self.assertEqual(req.get_method(), 'DELETE')
            expected_path = 'http://127.0.0.1:8080/v1.0/fake/volumes/' + \
                    'volid/export?force=True'
            self.assertEqual(req.get_full_url(), expected_path)
            return MockResponse()
        self.set_response(export_delete_force)
        resp = c.exports.delete('volid', force=True)
        self.assertTrue(export_delete_force.called)


if __name__ == "__main__":
    unittest.main()
