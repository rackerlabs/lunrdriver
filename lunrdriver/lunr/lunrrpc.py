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

try:
    # Register global cinder opts in Havana
    from cinder.common import config
except ImportError:
    # Grizzly
    pass

from cinder import db, quota, exception
from cinder.image import glance
from cinder.openstack.common import excutils
from cinder.openstack.common import timeutils, log as logging
from cinder.volume import volume_types
from cinder.context import get_admin_context

from lunr.cinder.client import LunrClient, LunrError
from lunr.cinder.utils import initialize_connection, terminate_connection, \
        attach_volume, detach_volume
from lunr.cinder.flags import CONF


LOG = logging.getLogger('cinder.lunr.lunrrpc')
QUOTAS = quota.QUOTAS


class LunrRPC(object):

    def __init__(self):
        pass

    def create_consumer(self, *args, **kwargs):
        pass

    def consume_in_thread(self, *args, **kwargs):
        pass

    def _get_volume_type_id(self, volume_type_name):
        try:
            return volume_types.get_volume_type_by_name(
                get_admin_context(), volume_type_name)['id']
        except exception.VolumeTypeNotFoundByName:
            LOG.exception("Unknown volume type '%s';"
                          " not valid volume type?" % volume_type_name)
            raise

    def create_volume(self, context, volume_id, snapshot_id=None,
                      image_id=None, source_volid=None, **kwargs):
        context = context.elevated()
        volume = db.volume_get(context, volume_id)
        LOG.info(_("volume %s: creating"), volume['name'])
        model_update = {'host': 'lunr'}
        volume['host'] = 'lunr'
        try:
            # Try to get the volume type name, else use the default volume type
            volume_type_name = volume['volume_type']['name']
        except (KeyError, TypeError):
            volume_type_name = CONF.lunr_default_volume_type
            # Using the default volume type name,
            # ask the db for the volume type id
            vtype_id = self._get_volume_type_id(volume_type_name)
            model_update['volume_type_id'] = vtype_id
            volume['volume_type_id'] = vtype_id

        db.volume_update(context, volume['id'], model_update)

        params = {
            'name': volume['id'],
            'size': volume['size'],
            'volume_type_name': volume_type_name,
        }

        # Copy image to volume!
        if image_id:
            params['image_id'] = image_id
            image_service, image_id = glance.get_remote_image_service(context,
                                                                      image_id)
            image_meta = image_service.show(context, image_id)
            if image_meta:
                db.volume_glance_metadata_create(context, volume['id'],
                                                 'image_id', image_id)
                name = image_meta.get('name', None)
                if name:
                    db.volume_glance_metadata_create(context, volume['id'],
                                                     'image_name', name)
                image_properties = image_meta.get('properties', {})
                for key, value in image_properties.items():
                    db.volume_glance_metadata_create(context, volume['id'],
                                                     key, value)

        # If this is a snapshot request, add the backup param
        if snapshot_id:
            params['backup'] = snapshot_id
            snapshot_ref = db.snapshot_get(context, snapshot_id)
            original_vref = db.volume_get(context, snapshot_ref['volume_id'])
            if original_vref['bootable']:
                db.volume_glance_metadata_copy_to_volume(
                    context, volume_id, snapshot_id)
                db.volume_update(context, volume_id, {'bootable': True})

        # If this is a clone request, add the source_volume_id param
        if source_volid:
            params['source_volume'] = source_volid
            source_vref = db.volume_get(context, source_volid)
            if source_vref['bootable']:
                db.volume_glance_metadata_copy_from_volume_to_volume(
                    context, source_volid, volume_id)
                db.volume_update(context, volume_id, {'bootable': True})

        try:
            resp = LunrClient(volume, logger=LOG).volumes.create(
                volume['id'], **params)
        except LunrError as e:
            LOG.debug('error creating volume %s', volume['id'])
            # Don't leave an error'd volume around, the raise here
            # will notify the caller of the error (See Github Issue #343)
            # Also, in Havana, TaskFlow will revert the quota increase.
            db.volume_destroy(context, volume['id'])
            raise e

        model_update = {}
        if image_id:
            model_update['bootable'] = True
        if resp.body['size'] != volume['size']:
            model_update['size'] = resp.body['size']
        if resp.body['status'] == 'ACTIVE':
            model_update['status'] = 'available'
        model_update['launched_at'] = timeutils.utcnow()

        db.volume_update(context, volume['id'], model_update)

        # Add storage-node to the volume metadata
        db.volume_metadata_update(context, volume['id'],
                                  {'storage-node': resp.body['node_id']},
                                  False)

        LOG.debug(_("volume %s: created successfully"), volume['name'])
        return volume

    def delete_volume(self, context, volume_id):
        context = context.elevated()
        volume = db.volume_get(context, volume_id)
        try:
            LunrClient(volume, logger=LOG).volumes.delete(volume['id'])
        except LunrError as e:
            # ignore Not Found on delete
            if e.code == 404:
                LOG.debug(_("volume %s: already deleted"),
                          volume['id'])
            elif e.code == 409:
                db.volume_update(context,
                                   volume['id'],
                                   {'status': 'error'})
                LOG.debug(_("volume %s: volume is busy"), volume['id'])
                raise
            else:
                LOG.debug(_('error deleting volume %s'), volume['id'])
                db.volume_update(context,
                                 volume['id'],
                                 {'status': 'error_deleting'})
                raise
        try:
            reserve_opts = {'volumes': -1, 'gigabytes': -volume['size']}
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        volume['volume_type_id'])
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except Exception:
            reservations = None
            LOG.exception(_("Failed to update usages deleting volume"))
        db.volume_destroy(context, volume['id'])
        if reservations:
            QUOTAS.commit(context, reservations)
        LOG.debug(_("volume %s: deleted successfully"), volume['id'])
        return volume

    def create_snapshot(self, context, volume_id, snapshot_id):
        snapshot = db.snapshot_get(context, snapshot_id)
        context = context.elevated()
        client = LunrClient(snapshot, logger=LOG)
        params = {
            'volume': snapshot['volume_id']
        }
        try:
            client.backups.create(snapshot['id'], **params)
        except LunrError as e:
            LOG.debug(_('error creating snapshot %s'), snapshot_id)
            # Don't leave an error'd snapshot around, the raise here
            # will notify the caller of the error (See Github Issue #322)
            db.snapshot_destroy(context, snapshot['id'])
            raise

        vol_ref = db.volume_get(context, volume_id)
        if vol_ref['bootable']:
            db.volume_glance_metadata_copy_to_snapshot(
                context, snapshot_id, volume_id)

        return snapshot

    def delete_snapshot(self, context, snapshot_id):
        snapshot = db.snapshot_get(context, snapshot_id)
        context = context.elevated()
        LOG.debug(_("snapshot %s: deleting"), snapshot['name'])

        reserve_opts = {'snapshots': -1}
        volume = db.volume_get(context, snapshot['volume_id'])
        try:
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        volume.get('volume_type_id'))
            reservations = QUOTAS.reserve(context,
                                          **reserve_opts)
        except Exception:
            reservations = None
            LOG.exception(_("Failed to update usages deleting snapshot"))

        client = LunrClient(snapshot, logger=LOG)
        try:
            client.backups.delete(snapshot['id'])
        except LunrError as e:
            # ignore Not Found on delete_snapshot. Don't wait on status.
            if e.code == 404:
                db.snapshot_destroy(context, snapshot['id'])
                LOG.debug(_("snapshot %s: deleted successfully"),
                          snapshot['name'])
            elif e.code == 409:
                db.snapshot_update(context,
                                   snapshot['id'],
                                   {'status': 'available'})
                LOG.debug(_("snapshot %s: snapshot is busy"), snapshot['name'])
                if reservations:
                    QUOTAS.rollback(context, reservations)
                raise
            else:
                LOG.debug(_('error deleting snapshot %s'), snapshot['id'])
                db.snapshot_update(context,
                                   snapshot['id'],
                                   {'status': 'error_deleting'})
                if reservations:
                    QUOTAS.rollback(context, reservations)
                raise

        if reservations:
            QUOTAS.commit(context, reservations)

        return snapshot

    def attach_volume(self, context, volume_id, instance_uuid, host_name, mountpoint, mode):
        volume = db.volume_get(context, volume_id)
        client = LunrClient(volume, logger=LOG)
        attach_volume(client, volume_id, instance_uuid, mountpoint)
        db.volume_attached(context.elevated(),
                           volume_id,
                           instance_uuid,
                           host_name,
                           mountpoint)

    def detach_volume(self, context, volume_id):
        volume = db.volume_get(context, volume_id)
        client = LunrClient(volume, logger=LOG)

        detach_volume(client, volume_id)
        db.volume_detached(context.elevated(), volume_id)

    def initialize_connection(self, context, volume_id, connector):
        volume = db.volume_get(context, volume_id)
        client = LunrClient(volume, logger=LOG)
        connection_info = initialize_connection(client, volume_id)
        LOG.debug("connection_info = %r" % connection_info)
        return connection_info

    def terminate_connection(self, context, volume_id, connector, force=False):
        volume = db.volume_get(context, volume_id)
        client = LunrClient(volume, logger=LOG)
        terminate_connection(client, volume_id, force=force)


conn = None


def create_connection(conf, new=True):
    global conn
    if not conn:
        conn = LunrRPC()
    return conn

def call(conf, context, topic, msg, timeout=None):
    LOG.debug("call, msg: %s" % msg)
    rpc = LunrRPC()
    method_name = msg['method']
    args = msg['args']
    args.pop('topic', None)
    method = getattr(rpc, method_name)
    return method(context, **args)

def cast(conf, context, topic, msg):
    LOG.debug("cast, msg: %s" % msg)
    rpc = LunrRPC()
    method_name = msg['method']
    args = msg['args']
    args.pop('topic', None)
    method = getattr(rpc, method_name)
    method(context, **args)

def cleanup():
    pass





