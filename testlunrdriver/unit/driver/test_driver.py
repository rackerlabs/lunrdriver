#!/usr/bin/env python

import __builtin__
setattr(__builtin__, '_', lambda x: x)

import unittest
from collections import namedtuple
from datetime import datetime, timedelta
import os
import errno
from StringIO import StringIO
from urlparse import urlparse
from urllib2 import HTTPError, URLError
from cinder.exception import VolumeTypeNotFoundByName
from cinder.volume import configuration as conf
import json
from uuid import uuid4

from lunrdriver.driver import utils
from lunrdriver.driver import driver
from lunrdriver.lunr import client

from testlunrdriver.unit.driver import ClientTestCase, urldecode, patch


def no_sleep(*args):
    pass


def date_string(offset):
    """
    retunrs a string like "2012-06-05 20:10:35"

    :param offset: days from now (can be negative)
    """
    ts = datetime.now() - timedelta(offset)
    return str(ts).rsplit('.', 1)[0]


class MockVolumeTypes(object):

    def __init__(self):
        self.store = {
            'vtype': {'id': 1}
        }
        self.duplicate_create_types = []

    def get_volume_type_by_name(self, context, name):
        return self.store[name]

    def create(self, context, name, extra_specs={}):
        if name in self.store:
            self.duplicate_create_types.append(name)
            raise driver.exception.VolumeTypeExists(
                'volume type %s already exists' % name)
        self.store[name] = {
            'id': len(self.store) + 1,
        }

class DriverTestCase(ClientTestCase):

    def setUp(self):
        super(DriverTestCase, self).setUp()
        self._orig_volume_types = driver.volume_types
        self.volume_types = MockVolumeTypes()
        driver.volume_types = self.volume_types
        self.configuration = conf.Configuration([])
        self.connector = {'ip': '127.0.0.1'}

    def tearDown(self):
        super(DriverTestCase, self).tearDown()
        driver.volume_types = self._orig_volume_types


class TestLunrDriver(DriverTestCase):

    def test_create_driver_instance(self):
        d = driver.LunrDriver(configuration=self.configuration)
        self.assertEquals(d.url, d.configuration.lunr_api_endpoint)

    def test_create_volume(self):
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'}}
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['size'], '1')
            self.assertEquals(data['volume_type_name'], 'vtype')
        self.request_callback = callback
        self.resp = [json.dumps({'size': 1, 'cinder_host': 'foo'})]
        d = driver.LunrDriver(configuration=self.configuration)
        update = d.create_volume(volume)
        self.assert_(self.request_callback.called)
        self.assertEquals(update['host'], 'foo')

    def test_create_volume_duplicate(self):
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'}}
        lunr_id = []
        def callback(req):
            # First call gets the 409
            if len(self.request_callback.called) == 1:
                self.assertEquals(req.get_method(), 'PUT')
                url = urlparse(req.get_full_url())
                self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
                return
            # We need to capture the lunr_id from the retry
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            data = urldecode(url.query)
            self.assertEquals(data['name'], '123-456')
            # Lazy way to find the new id
            lunr_id.append(url.path[1+url.path.rfind('/'):])
        self.request_callback = callback

        err = HTTPError('/v1.0/100/volumes/123-456', 409, 'Server Error', {},
                        StringIO('{"reason": "conflict"}'))
        self.resp = [err, json.dumps({'size': 1, 'cinder_host': 'foo'})]
        d = driver.LunrDriver(configuration=self.configuration)
        update = d.create_volume(volume)
        self.assertEquals(len(self.request_callback.called), 2)
        self.assertEquals(1, len(lunr_id))
        self.assertEquals(update['_name_id'], lunr_id[0])

    def test_create_volume_with_meta(self):
        MetaEntry = namedtuple('MetaEntry', ['key', 'value'])
        meta = MetaEntry('foo', 'bar')
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'},
                  'volume_metadata': [meta]}
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['size'], '1')
            self.assertEquals(data['volume_type_name'], 'vtype')
        self.request_callback = callback
        self.resp = [json.dumps({'size': 1, 'cinder_host': 'foo',
                                 'node_id': 'nodeuuid'})]
        d = driver.LunrDriver(configuration=self.configuration)
        update = d.create_volume(volume)
        self.assert_(self.request_callback.called)
        self.assertEquals(update['host'], 'foo')
        self.assertEquals(update['metadata'], {'foo': 'bar',
                                               'storage-node': 'nodeuuid'})

    def test_create_volume_with_affinity(self):
        MetaEntry = namedtuple('MetaEntry', ['key', 'value'])
        meta = MetaEntry('different_node', 'foo,bar,baz')
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'},
                  'volume_metadata': [meta]}
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['size'], '1')
            self.assertEquals(data['affinity'], 'different_node:foo,bar,baz')
            self.assertEquals(data['volume_type_name'], 'vtype')
        self.request_callback = callback
        self.resp = [json.dumps({'size': 1, 'cinder_host': 'foo',
                                 'node_id': 'nodeuuid'})]
        d = driver.LunrDriver(configuration=self.configuration)
        update = d.create_volume(volume)
        self.assert_(self.request_callback.called)
        self.assertEquals(update['host'], 'foo')
        self.assertEquals(update['metadata'], {'different_node': 'foo,bar,baz',
                                               'storage-node': 'nodeuuid'})

    def test_create_volume_with_maintenance_zone(self):
        MetaEntry = namedtuple('MetaEntry', ['key', 'value'])
        meta = MetaEntry('maintenance_zone', 'foobar')
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'},
                  'volume_metadata': [meta]}
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['size'], '1')
            self.assertEquals(data['maintenance_zone'], 'foobar')
            self.assertEquals(data['volume_type_name'], 'vtype')
        self.request_callback = callback
        self.resp = [json.dumps({'size': 1, 'cinder_host': 'foo',
                                 'node_id': 'nodeuuid'})]
        d = driver.LunrDriver(configuration=self.configuration)
        update = d.create_volume(volume)
        self.assert_(self.request_callback.called)
        self.assertEquals(update['host'], 'foo')
        self.assertEquals(update['metadata'], {'maintenance_zone': 'foobar',
                                               'storage-node': 'nodeuuid'})

    def test_create_volume_from_snapshot(self):
        volume = {'name': 'vol1', 'size': 5, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'}}
        snapshot = {'name': 'backup1', 'id': '456-789'}
        def callback(req):
            if len(self.request_callback.called) > 1:
                self.assertEquals(req.get_method(), 'GET')
                return
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % '123-456')
            data = urldecode(url.query)
            self.assertEquals(data['volume_type_name'], 'vtype')
            self.assertEquals(data['backup'], snapshot['id'])
        self.request_callback = callback
        building_status = json.dumps({
            'status': 'BUILDING',
        })
        active_status = json.dumps({
            'status': 'ACTIVE',
        })
        self.resp = [json.dumps({'size': 1, 'cinder_host': 'foo'}),
                     building_status, active_status]
        d = driver.LunrDriver(configuration=self.configuration)
        with patch(client, 'sleep', no_sleep):
            update = d.create_volume_from_snapshot(volume, snapshot)
        self.assertEquals(len(self.request_callback.called), 3)
        self.assertEquals(update, {'size': 1, 'host': 'foo'})

    def test_create_cloned_volume(self):
        volume = {'name': 'vol1', 'size': 5, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'}}
        source = {'name': 'vol2', 'size': 5, 'project_id': 100,
                  'id': '234-567', 'volume_type': {'name': 'vtype'}}
        def callback(req):
            if len(self.request_callback.called) > 1:
                self.assertEquals(req.get_method(), 'GET')
                return
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['volume_type_name'], 'vtype')
            self.assertEquals(data['source_volume'], '234-567')
        self.request_callback = callback
        building_status = json.dumps({
            'status': 'BUILDING',
        })
        active_status = json.dumps({
            'status': 'ACTIVE',
        })
        self.resp = [json.dumps({'size': 1}), building_status, active_status]
        d = driver.LunrDriver(configuration=self.configuration)
        with patch(client, 'sleep', no_sleep):
            d.create_cloned_volume(volume, source)
        self.assertEquals(len(self.request_callback.called), 3)

    def test_clone_image(self):
        volume = {'name': 'vol1', 'size': 5, 'project_id': 100,
                  'id': '123-456', 'volume_type': {'name': 'vtype'}}
        image_location = "somewhere over the rainbow"
        image_meta = {'id': 'image_id_1'}
        def callback(req):
            if len(self.request_callback.called) > 1:
                self.assertEquals(req.get_method(), 'GET')
                return
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
            data = urldecode(url.query)
            self.assertEquals(data['image_id'], image_meta['id'])
        self.request_callback = callback
        building_status = json.dumps({
            'status': 'BUILDING',
        })
        active_status = json.dumps({
            'status': 'ACTIVE',
        })
        self.resp = [json.dumps({'size': 1}), building_status, active_status]
        d = driver.LunrDriver(configuration=self.configuration)
        with patch(client, 'sleep', no_sleep):
            d.clone_image('unused', volume, image_location, image_meta,
                          'image_service')
        self.assertEquals(len(self.request_callback.called), 3)

    def test_failed_volume_create(self):
        # TODO: resp should be URLError'y
        self.resp = Exception('kaboom!')
        d = driver.LunrDriver(configuration=self.configuration)
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '234-567', 'volume_type': {'name': 'vtype'}}
        self.assertRaises(Exception, d.create_volume, volume)
        self.assert_(self.request_callback.called)

    def test_delete_volume(self):
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '345-678', 'volume_type': {'name': 'vtype'}}
        def callback(req):
            self.assertEquals(req.get_method(), 'DELETE')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/100/volumes/%s' % volume['id'])
        self.request_callback = callback
        self.resp = [json.dumps({'status': 'DELETING'})]
        d = driver.LunrDriver(configuration=self.configuration)
        d.delete_volume(volume)
        self.assert_(self.request_callback.called)

    def test_failed_delete_connection_error(self):
        self.resp = URLError(OSError(errno.ECONNREFUSED,
                                     os.strerror(errno.ECONNREFUSED)))
        d = driver.LunrDriver(configuration=self.configuration)
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '456-789', 'volume_type': {'name': 'vtype'}}
        self.assertRaises(client.LunrError, d.delete_volume, volume)
        self.assert_(self.request_callback.called)

    def test_failed_delete_server_error(self):
        # url, code, msg, hdrs, fp
        self.resp = HTTPError('/v1.0/100/volumes/456-789', 500, 'Server Error', {},
                              StringIO('{"reason": "not found"}'))
        d = driver.LunrDriver(configuration=self.configuration)
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '456-789', 'volume_type': {'name': 'vtype'}}
        self.assertRaises(client.LunrError, d.delete_volume, volume)
        self.assert_(self.request_callback.called)

    def test_success_delete_not_found(self):
        # url, code, msg, hdrs, fp
        self.resp = HTTPError('/v1.0/100/volumes/456-789', 404, 'Not Found', {},
                              StringIO('{"reason": "not found"}'))
        d = driver.LunrDriver(configuration=self.configuration)
        volume = {'name': 'vol1', 'size': 1, 'project_id': 100,
                  'id': '456-789', 'volume_type': {'name': 'vtype'}}
        d.delete_volume(volume)
        self.assert_(self.request_callback.called)

    def test_success_delete_snapshot_not_found(self):
        # url, code, msg, hdrs, fp
        self.resp = HTTPError('/v1.0/100/backups/s456-789', 404, 'Not Found',
                              {}, StringIO('{"reason": "not found"}'))
        d = driver.LunrDriver(configuration=self.configuration)
        snapshot = {'project_id': 100, 'id': 's456-789'}
        d.delete_snapshot(snapshot)
        self.assert_(self.request_callback.called)

    def test_initialize_connection(self):
        volume = {'id': 1, 'name': 'vol1', 'project_id': 'dev'}
        self.resp = json.dumps({
            'id': 'vol1',
            'target_portal': 'lunr1:3260',
            'target_name': 'iqn-vol1',
        })
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path,
                              '/v1.0/dev/volumes/%s/export' % volume['id'])
        self.request_callback = callback
        d = driver.LunrDriver(configuration=self.configuration)
        _orig_gethostbyname = utils.socket.gethostbyname
        try:
            utils.socket.gethostbyname = lambda *args: '10.0.0.1'
            connection_info = d.initialize_connection(volume, self.connector)
        finally:
            utils.socket.gethostbyname = _orig_gethostbyname
        self.assert_(self.request_callback.called)
        expected = {
            'driver_volume_type': 'iscsi',
            'data': {
                'target_discovered': False,
                'target_iqn': 'iqn-vol1',
                'target_portal': '10.0.0.1:3260',
                'volume_id': 1,
            }
        }
        self.assertEquals(connection_info, expected)

    def test_target_portal_is_ip(self):
        volume = {'id': 1, 'name': 'vol1', 'project_id': 'dev'}
        self.resp = json.dumps({
            'id': 'vol1',
            'target_portal': '10.0.0.2:3260',
            'target_name': 'iqn-vol1',
        })
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path,
                              '/v1.0/dev/volumes/%s/export' % volume['id'])
        self.request_callback = callback
        d = driver.LunrDriver(configuration=self.configuration)
        _orig_gethostbyname = utils.socket.gethostbyname
        try:
            def mock_gethostbyname(*args):
                raise Exception('driver should not call gethostbyname on ip')
            utils.socket.gethostbyname = mock_gethostbyname
            connection_info = d.initialize_connection(volume, self.connector)
        finally:
            utils.socket.gethostbyname = _orig_gethostbyname
        self.assert_(self.request_callback.called)
        expected = {
            'driver_volume_type': 'iscsi',
            'data': {
                'target_discovered': False,
                'target_iqn': 'iqn-vol1',
                'target_portal': '10.0.0.2:3260',
                'volume_id': 1,
            }
        }
        self.assertEquals(connection_info, expected)

    def test_gethostbyname_lookup_fails(self):
        volume = {'id': 1, 'name': 'vol1', 'project_id': 'dev'}
        hostname = uuid4().hex
        self.resp = json.dumps({
            'id': 'vol1',
            'target_portal': '%s:3260' % hostname,
            'target_name': 'iqn-vol1',
        })
        def callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path,
                              '/v1.0/dev/volumes/%s/export' % volume['id'])
        self.request_callback = callback
        d = driver.LunrDriver(configuration=self.configuration)
        _orig_gethostbyname = utils.socket.gethostbyname
        try:
            def mock_gethostbyname(*args):
                raise utils.socket.gaierror(-5, 'No address associated with hostname')
            utils.socket.gethostbyname = mock_gethostbyname
            connection_info = d.initialize_connection(volume, self.connector)
        finally:
            utils.socket.gethostbyname = _orig_gethostbyname
        self.assert_(self.request_callback.called)
        expected = {
            'driver_volume_type': 'iscsi',
            'data': {
                'target_discovered': False,
                'target_iqn': 'iqn-vol1',
                'target_portal': '%s:3260' % hostname,
                'volume_id': 1,
            }
        }
        self.assertEquals(connection_info, expected)

    def test_create_snapshot_success(self):
        # args
        snapshot = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'project_id': 'dev'
        }

        # mock response chain
        create_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'SAVING',
        }
        saving_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'SAVING',
        }
        ready_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'AVAILABLE',
        }
        self.resp = [json.dumps(resp) for resp in (
            create_response, saving_response, ready_response)]

        # setup request verification stack
        def create_callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')
            data = urldecode(url.query)
            self.assertEquals(data['volume_id'], '0000-0001')

        def saving_callback(req):
            self.assertEquals(req.get_method(), 'GET')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')

        def ready_callback(req):
            self.assertEquals(req.get_method(), 'GET')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')

        callbacks = [create_callback, saving_callback, ready_callback]
        def request_callback(req):
            callback = callbacks.pop(0)
            callback(req)

        class MockDB:
            def volume_get(self, ctx, volume_id):
                return {'id': volume_id}

        d = driver.LunrDriver(configuration=self.configuration)
        d.db = MockDB()
        with patch(client, 'sleep', no_sleep):
            d.create_snapshot(snapshot)
        self.assertEquals(len(self.request_callback.called), 3)

    def test_create_snapshot_errors(self):
        # args
        snapshot = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'project_id': 'dev'
        }

        # mock response chain
        create_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'SAVING',
        }
        saving_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'SAVING',
        }
        error_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'ERROR',
        }
        self.resp = [json.dumps(resp) for resp in (
            create_response, saving_response, error_response)]

        # setup request verification stack
        def create_callback(req):
            self.assertEquals(req.get_method(), 'PUT')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')
            data = urldecode(url.query)
            self.assertEquals(data['volume_id'], '0000-0001')

        def saving_callback(req):
            self.assertEquals(req.get_method(), 'GET')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')

        def ready_callback(req):
            self.assertEquals(req.get_method(), 'GET')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')

        callbacks = [create_callback, saving_callback, ready_callback]
        def request_callback(req):
            callback = callbacks.pop(0)
            callback(req)

        class MockDB:
            def volume_get(self, ctx, volume_id):
                return {'id': volume_id}

        d = driver.LunrDriver(configuration=self.configuration)
        d.db = MockDB()
        with patch(client, 'sleep', no_sleep):
            self.assertRaises(client.StatusError, d.create_snapshot, snapshot)
        self.assertEquals(len(self.request_callback.called), 3)

    def test_delete_snapshot_success(self):
        # args
        snapshot = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'project_id': 'dev'
        }

        # mock response chain
        delete_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'DELETING',
        }
        resp = [json.dumps(delete_response)]
        get_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'DELETING',
        }
        resp.append(json.dumps(get_response))
        get_response = {
            'id': '0000-0000',
            'volume_id': '0000-0001',
            'status': 'AUDITING',
        }
        resp.append(json.dumps(get_response))
        self.resp = resp

        # setup request verification stack
        def delete_callback(req):
            self.assertEquals(req.get_method(), 'DELETE')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/dev/backups/0000-0000')
            data = urldecode(url.query)
            self.assertEquals(data['volume_id'], '0000-0001')

        callbacks = [delete_callback]
        def request_callback(req):
            callback = callbacks.pop(0)
            callback(req)

        d = driver.LunrDriver(configuration=self.configuration)
        with patch(client, 'sleep', no_sleep):
            d.delete_snapshot(snapshot)
        self.assertEquals(len(self.request_callback.called), 3)

    def test_check_for_setup_error(self):
        # setup mock response
        vtype1 = {
            'name': 'vtype1',
            'status': 'ACTIVE',
            'min_size': 0,
            'max_size': 10,
            'created_at': date_string(-10),
            'last_modified': date_string(-2),
        }
        vtype2 = {
            'name': 'vtype2',
            'status': 'DELETED',
            'min_size': 100,
            'max_size': 0,
            'created_at': date_string(-20),
            'last_modified': date_string(-10),
        }
        self.resp = [json.dumps([vtype1, vtype2])]
        # setup verify request callback
        def request_callback(req):
            self.assertEquals(req.get_method(), 'GET')
            url = urlparse(req.get_full_url())
            self.assertEquals(url.path, '/v1.0/admin/volume_types')
        self.request_callback = request_callback
        d = driver.LunrDriver(configuration=self.configuration)
        # mock cinder db call
        class MockVolumeTypeApi(object):

            def __init__(self):
                self.types = []

            def create(self, context, name, extra_specs={}):
                self.types.append(name)

        mock_volume_type_api = MockVolumeTypeApi()

        with patch(driver, 'volume_types', mock_volume_type_api):
            d.check_for_setup_error()
        self.assert_(self.request_callback.called)
        self.assert_(mock_volume_type_api.types, ['vtype1'])

    def test_volume_type_already_exists(self):
        # setup mock response
        vtype1 = {
            'name': 'vtype1',
            'status': 'ACTIVE',
            'min_size': 0,
            'max_size': 10,
            'created_at': date_string(-10),
            'last_modified': date_string(-2),
        }
        vtype2 = {
            'name': 'vtype2',
            'status': 'ACTIVE',
            'min_size': 0,
            'max_size': 100,
            'created_at': date_string(-10),
            'last_modified': date_string(-2),
        }
        self.resp = json.dumps([vtype1, vtype2])

        # mock out volume type api calls
        class MockVolumeTypeApi(object):

            def __init__(self):
                self.types = ['vtype1']
                self.duplicate_create_types = []
            def create(self, context, name, extra_specs={}):
                if name in self.types:
                    self.duplicate_create_types.append(name)
                    raise driver.exception.VolumeTypeExists(
                        'volume type %s already exists' % name)
                self.types.append(name)

        mock_volume_type_api = MockVolumeTypeApi()

        d = driver.LunrDriver(configuration=self.configuration)
        with patch(driver, 'volume_types', mock_volume_type_api):
            d.check_for_setup_error()
            self.assert_(self.request_callback.called)
            self.assert_(mock_volume_type_api.types, ['vtype1', 'vtype2'])
            self.assert_(mock_volume_type_api.duplicate_create_types, ['vtype1'])
            # call again
            self.resp = json.dumps([vtype1, vtype2])
            d.check_for_setup_error()
            self.assert_(mock_volume_type_api.types, ['vtype1', 'vtype2'])
            expected = ['vtype1',  # from first run
                       'vtype1', 'vtype2']  # second time both raise
            self.assert_(mock_volume_type_api.duplicate_create_types, expected)

    def test_check_for_setup_error_fails(self):
        d = driver.LunrDriver(configuration=self.configuration)
        # three unable to connects
        err = URLError(
            OSError(
                errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED)
            )
        ) 
        self.resp = [err for i in range(3)]
        with patch(driver, 'sleep', no_sleep):
            d.check_for_setup_error()
        # two errors, and one success!
        vtype = {
            'name': 'new_type',
            'status': 'ACTIVE',
            'read_iops': 1000,
            'write_iops': 1000,
            'min_size': 0,
            'max_size': 100,
            'created_at': date_string(-10),
            'last_modified': date_string(-2),
        }
        self.resp = [err, err, json.dumps([vtype])]
        with patch(driver, 'sleep', no_sleep):
            d.check_for_setup_error()
            self.assert_('new_type' in self.volume_types.store)


if __name__ == "__main__":
    unittest.main()
