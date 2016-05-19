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


from cinder.volume.api import API as CinderAPI
try:
    from cinder.i18n import _
except ImportError:
    pass

from cinder import exception
try:
    from oslo_log import log as logging
except ImportError:
    from cinder.openstack.common import log as logging
from cinder.volume import volume_types
from lunrdriver.lunr.client import LunrClient, LunrError
from lunrdriver.lunr.flags import CONF


LOG = logging.getLogger('cinder.lunr.api')


class SnapshotConflict(exception.Invalid):
    message = _("Existing snapshot operation on volume %(volume_id)s in "
            "progress, please retry.")
    code = 409


class API(CinderAPI):

    def _is_lunr_volume_type(self, context, volume_type):
        if not volume_type:
            return False
        if isinstance(volume_type, basestring):
            volume_type = self.db.volume_type_get(context, volume_type)
        return volume_type['name'] in CONF.lunr_volume_types

    def _validate_lunr_volume_type(self, volume_type, size):
        if not volume_type:
            return

        lunr_context = {'project_id': 'admin'}
        try:
            client = LunrClient(CONF.lunr_api_endpoint,
                                lunr_context, logger=LOG)
            resp = client.types.get(volume_type['name'])
        except LunrError as e:
            LOG.error(_('unable to fetch volume type from LunR: %s'),
                      volume_type)
            raise

        try:
            size = int(size)
        except ValueError:
            raise exception.InvalidInput(reason=_("'size' parameter must be "
                                                  "an integer"))
        if resp.body:
            if size < resp.body['min_size'] or size > resp.body['max_size']:
                msg = _("'size' parameter must be between "
                        "%s and %s") % (resp.body['min_size'],
                                        resp.body['max_size'])
                raise exception.InvalidInput(reason=msg)

    def create(self, context, size, name, description, snapshot=None,
               image_id=None, volume_type=None, metadata=None,
               availability_zone=None, source_volume=None,
               scheduler_hints=None,
               source_replica=None, consistencygroup=None,
               cgsnapshot=None, multiattach=False, source_cg=None):

        if not volume_type:
            volume_type = volume_types.get_default_volume_type()

        if self._is_lunr_volume_type(context, volume_type):
            # Lunr has size limits by volume type. Fail here instead of
            # getting an 'error' volume.
            self._validate_lunr_volume_type(volume_type, size)

            if not CONF.lunr_copy_image_enabled:
                image_id = None
            if not CONF.lunr_volume_clone_enabled:
                source_volume = None
            if snapshot:
                if self._is_lunr_volume_type(context,
                                             snapshot['volume_type_id']):
                    snapshot['volume_type_id'] = volume_type['id']
            if source_volume:
                if self._is_lunr_volume_type(context,
                                             source_volume['volume_type_id']):
                    source_volume['volume_type_id'] = volume_type['id']

        kwargs = {}
        if snapshot is not None:
            kwargs['snapshot'] = snapshot
        if image_id is not None:
            kwargs['image_id'] = image_id
        if volume_type is not None:
            kwargs['volume_type'] = volume_type
        if metadata is not None:
            kwargs['metadata'] = metadata
        if availability_zone is not None:
            kwargs['availability_zone'] = availability_zone
        if source_volume is not None:
            kwargs['source_volume'] = source_volume
        if scheduler_hints is not None:
            kwargs['scheduler_hints'] = scheduler_hints
        if multiattach is not None:
            kwargs['multiattach'] = multiattach
        if source_replica is not None:
            kwargs['source_replica'] = source_replica
        if consistencygroup is not None:
            kwargs['consistencygroup'] = consistencygroup
        if cgsnapshot is not None:
            kwargs['cgsnapshot'] = cgsnapshot
        if source_cg is not None:
            kwargs['source_cg'] = source_cg

        return super(API, self).create(context, size, name, description,
                                       **kwargs)

    def delete(self, context, volume, force=False):
        if self._is_lunr_volume_type(context, volume['volume_type_id']):
            # Cinder doesn't let you delete in 'error_deleting' but that is
            # ridiculous, so go ahead and mark it 'error'.
            if volume['status'] == 'error_deleting':
                volume['status'] = 'error'
                self.db.volume_update(context, volume['id'],
                                      {'status': 'error'})

        return super(API, self).delete(context, volume, force)

    def _check_snapshot_conflict(self, context, volume):
        # This is a stand in for Lunr's 409 conflict on a volume performing
        # multiple snapshot operations. It doesn't work in all cases,
        # but is better than nothing.
        if not self._is_lunr_volume_type(context, volume['volume_type_id']):
            return

        siblings = self.db.snapshot_get_all_for_volume(context, volume['id'])
        for snap in siblings:
            if snap['status'] in ('creating', 'deleting'):
                raise SnapshotConflict(reason="Snapshot conflict",
                                       volume_id=volume['id'])

    def _create_snapshot(self, context, volume, name, description, force=False,
                         metadata=None, cgsnapshot_id=None):
        if not force:
            self._check_snapshot_conflict(context, volume)
        kwargs = {}
        kwargs['force'] = force
        if metadata is not None:
            kwargs['metadata'] = metadata
        if cgsnapshot_id is not None:
            kwargs['cgsnapshot_id'] = cgsnapshot_id
        return super(API, self)._create_snapshot(context, volume, name,
                                                 description, **kwargs)

    def delete_snapshot(self, context, snapshot, force=False):
        if self._is_lunr_volume_type(context, snapshot['volume_type_id']):
            # Cinder doesn't let you delete in 'error_deleting' but that is
            # ridiculous, so go ahead and mark it 'error'.
            if snapshot['status'] == 'error_deleting':
                snapshot['status'] = 'error'
                self.db.snapshot_update(context, snapshot['id'],
                                        {'status': 'error'})
            if not force:
                volume = self.db.volume_get(context, snapshot['volume_id'])
                self._check_snapshot_conflict(context, volume)
        return super(API, self).delete_snapshot(context, snapshot, force)
