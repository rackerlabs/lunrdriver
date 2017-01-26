import json
import socket

from six.moves.http_client import HTTPException, BadStatusLine
from six.moves.urllib.parse import urlencode
from six.moves.urllib.request import Request, urlopen
from six.moves.urllib.error import URLError, HTTPError
from webob import exc

try:
    from eventlet import sleep
except ImportError:
    from time import sleep

try:
    from oslo_log import log as logging
except ImportError:
    from cinder.openstack.common import log as logging

try:
    import oslo_context
except ImportError:
    from cinder.openstack.common import local


LOG = logging.getLogger('cinder.volume.lunr.client')


def request_id():
    try:
        return oslo_context.context.get_current().request_id
    except NameError:
        return local.store.context.request_id


class StatusError(Exception):
    pass


class LunrResource(object):
    """
    Base class for Lunr api resource CRUD.

    Concrete classes need to define a `resource_path` attribute for the
    benifit of `get_path`.
    """

    def __init__(self, client):
        self.client = client

    def get_path(self, _id=None):
        if _id:
            return self.resource_path + '/%s' % _id
        return self.resource_path

    def get(self, _id):
        return self.client._execute('GET', self.get_path(_id))

    def list(self, **kwargs):
        return self.client._execute('GET', self.get_path(), **kwargs)

    def create(self, _id, **params):
        return self.client._execute('PUT', self.get_path(_id), **params)

    def delete(self, _id):
        return self.client._execute('DELETE', self.get_path(_id))

    def wait_on_status(self, _id, *statuses):
        if not statuses:
            raise ValueError("No statuses supplied")
        backoff = 1
        max_backoff = 30
        while True:
            resp = self.get(_id)
            if resp.body['status'] in statuses:
                return resp
            if resp.body['status'].endswith('ING'):
                sleep(backoff)
                backoff *= 2
                if backoff > max_backoff:
                    backoff = max_backoff
            else:
                raise StatusError('resource entered %s status while waiting '
                                'on %s' % (resp.body['status'], statuses))


class LunrVolumeResource(LunrResource):

    resource_path = 'volumes'

    def update(self, _id, **params):
        return self.client._execute('POST', self.get_path(_id), **params)


class LunrExportResource(LunrResource):

    def get_path(self, _id):
        return 'volumes/%s/export' % _id

    def update(self, _id, **params):
        return self.client._execute('POST', self.get_path(_id), **params)

    def delete(self, _id, force=False, **params):
        if force:
            return self.client._execute('DELETE', self.get_path(_id),
                                        force=True, **params)
        return self.client._execute('DELETE', self.get_path(_id), **params)


class LunrBackupResource(LunrResource):

    resource_path = 'backups'


class LunrTypeResource(LunrResource):

    resource_path = 'volume_types'


class LunrError(Exception):
    # Catch IOError to handle uncaught SSL Errors
    exceptions = (URLError, HTTPException, HTTPError, IOError)

    title = exc.HTTPServiceUnavailable.title
    code = exc.HTTPServiceUnavailable.code
    _explanation = exc.HTTPServiceUnavailable.explanation

    def __init__(self, req, e):
        self.method = req.get_method()
        self.url = req.get_full_url()
        self.detail = "%s on %s " % (self.method, self.url)

        if isinstance(e, socket.timeout):
            self.detail += "failed with socket timeout"
            self.reason = self.detail

        if isinstance(e, HTTPError):
            raw_body = ''.join(e.fp.read())
            self.reason = raw_body  # most basic reason
            try:
                body = json.loads(raw_body)
            except ValueError:
                pass
            else:
                # json body has more info
                if 'reason' in body:
                    self.reason = body['reason']
                elif 'message' in body:
                    self.reason = body['message']
            self.detail += "returned '%s' with '%s'" % (e.code, self.reason)
            self.title = e.msg
            self.code = e.code

        if isinstance(e, URLError):
            self.detail += "failed with '%s'" % e.reason
            self.reason = e.reason

        if isinstance(e, IOError):
            self.detail += "failed with '%s'" % e
            self.reason = str(e)

        if isinstance(e, HTTPException):
            # work around urllib2 bug, it throws a
            # BadStatusLine without an explaination.
            if isinstance(e, BadStatusLine):
                self.detail += "failed with '%s'" % e.__class__.__name__
            else:
                self.detail += "failed with '%s'" % e
            self.reason = str(e)

    def __str__(self):
        return self.detail

    @property
    def explanation(self):
        if self.code // 100 == 4:
            # we're trying to pass something up to user
            return self.reason
        return self._explanation


class LunrClient(object):

    def __init__(self, url, context, logger=None):
        """
        Create a LunrClient object for the driver.

        :param url: Lunr endpoint url
        :param context: can be a cinder context, or volume - anything with a
                        project_id attribute.
        :param logger: optionally use the callers logger to tie client debug
                       messages to the component instead of the module.
        """
        try:
            self.project_id = context.project_id
        except AttributeError:
            self.project_id = context['project_id']
        self.logger = logger or LOG
        self.url = url
        self.volumes = LunrVolumeResource(self)
        self.exports = LunrExportResource(self)
        self.backups = LunrBackupResource(self)
        self.types = LunrTypeResource(self)

    def _execute(self, method, path, **kwargs):
        # TODO consider retrying on bad HTTP code
        path = '%s/%s/%s?%s' % (self.url,
                                self.project_id, path, urlencode(kwargs))
        try:
            headers = {'X-Request-Id': request_id()}
        except AttributeError:
            self.logger.warning('No threadlocal context!')
            headers = {}
        req = Request(path, headers=headers)
        req.get_method = lambda *args, **kwargs: method
        try:
            resp = urlopen(req)
            resp.body = json.loads(resp.read())
            self.logger.debug("%s on %s succeeded with %s" %
                (req.get_method(), req.get_full_url(), resp.getcode()))
            return resp
        except (HTTPError, URLError, HTTPException) as e:
            raise LunrError(req, e)
