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


import json
import time
from six.moves.urllib.request import Request, urlopen
from six.moves.urllib.error import HTTPError
from webob.exc import HTTPUnauthorized, HTTPServiceUnavailable

try:
    from oslo_log import log as logging
except ImportError:
    from cinder.openstack.common import log as logging

LOG = logging.getLogger('cinder.lunr.auth')


class InvalidUserToken(Exception):
    pass


class RackAuth(object):

    def __init__(self, conf, app):
        self.admin_user = conf.get('username', '')
        self.admin_pass = conf.get('password', '')
        self.admin_url = conf.get('url', '')
        self._admin_token = None
        self.app = app

    @property
    def admin_token(self):
        if not self._admin_token:
            admin_info = self._auth_request('/v2.0/tokens', method='POST',
                                            admin_request=True)
            self._admin_token = admin_info['access']['token']['id']
        LOG.debug('admin_token is %r' % self._admin_token)
        return self._admin_token

    def _auth_request(self, path, method='GET', admin_request=False):
        """Make a request to auth

        :returns : dump of json response body

        :raises : urllib2.HTTPError
        """
        attempts = 3
        attempt = 0
        while True:
            attempt += 1
            headers = {'Content-Type': 'application/json',
                       'Accept': 'application/json',
                       'User-Agent': 'CloudBlockStorage (RackAuth)'}
            body = None
            if admin_request:
                # this request is to get an admin token
                body = json.dumps({
                    "auth": {
                        "passwordCredentials": {
                            "username": self.admin_user,
                            "password": self.admin_pass,
                        }
                    }
                })
            else:
                # this request requires a validate admin token
                headers['X-Auth-Token'] = self.admin_token
            req_path = self.admin_url + path
            LOG.debug('req_path: %s - headers: %s' % (req_path, headers))
            req = Request(req_path, headers=headers, data=body)
            req.get_method = lambda *args: method
            try:
                resp = urlopen(req)
                return json.loads(resp.read())
            except HTTPError as e:
                if e.code == 401:
                    self._admin_token = None
                elif e.code == 404 and self._admin_token:
                    # 404 means invalid token, no retry
                    raise
            except Exception as e:
                pass
            if attempt >= attempts:
                raise
            LOG.exception('Failed validate token request, '
                          'attempt %s of %s' % (attempt, attempts))
            time.sleep(2 ** attempt)

    def get_token_info(self, token, account):
        """Check a token with auth

        :returns : an access dict

        e.g.

        token_info = {
            'access': {
                'token': {
                    'tenant': {
                        'name': 'account1',
                        'id': 'account1',
                    }
                },
                'user': {
                    'id': 'johnny', 'name': 'johnny',
                    'roles': [{'name': 'admin'}],
                }
            }
        }

        :raises : InvalidUserToken

        """
        path = '/v2.0/tokens/%s?belongsTo=%s' % (token, account)
        try:
            token_info = self._auth_request(path)
        except HTTPError as e:
            if e.code == 404:
                raise InvalidUserToken('token not found')
            else:
                raise

        LOG.debug('token_info: %r' % token_info)

        tenant_info = token_info['access']['token']['tenant']
        if account not in (tenant_info['name'], tenant_info['id']):
            raise InvalidUserToken('token does not match tenant')
        return token_info

    def get_headers(self, token_info):
        tenant_id = token_info['access']['token']['tenant']['id']
        tenant_name = token_info['access']['token']['tenant']['name']
        user_id = token_info['access']['user']['id']
        user_name = token_info['access']['user']['name']
        roles_list = token_info['access']['user'].get('roles', [])
        roles = ','.join([role['name'] for role in roles_list])

        headers = {
            'X-Identity-Status': 'Confirmed',
            'X-Tenant-Id': tenant_id,
            'X-Tenant-Name': tenant_name,
            'X-User-Id': user_id,
            'X-Role': roles,
        }

        return headers

    def __call__(self, environ, start_response):
        try:
            LOG.debug('path_info: %s' % environ['PATH_INFO'])
            account = environ['PATH_INFO'].lstrip('/').split('/', 1)[0]
            LOG.debug('account: %s' % account)
        except IndexError:
            LOG.debug('Unable to pull account from request path')
            return HTTPNotFound()(environ, start_response)
        try:
            token = environ['HTTP_X_AUTH_TOKEN']
        except KeyError:
            LOG.debug('Unable to pull token from request headers')
            return HTTPUnauthorized()(environ, start_response)
        try:
            LOG.debug('Validate token')
            token_info = self.get_token_info(token, account)
        except InvalidUserToken as e:
            LOG.info('Invalid token (%s)' % e)
            return HTTPUnauthorized()(environ, start_response)
        except Exception:
            LOG.exception('Unable to validate token')
            return HTTPServiceUnavailable()(environ, start_response)
        LOG.info('Token valid')
        headers = self.get_headers(token_info)
        LOG.debug('adding headers -> %r' % headers)
        for header, value in headers.items():
            environ['HTTP_' + header.upper().replace('-', '_')] = value
        return self.app(environ, start_response)


def filter_factory(global_conf, **local_conf):
    """Returns a callable that returns a piece of WSGI middleware."""

    def auth_filter(app):
        return RackAuth(local_conf, app)

    return auth_filter
