"""
Driver for LUNR volumes.
"""

from time import sleep

try:
    from oslo_config import cfg
except ImportError:
    from oslo.config import cfg

from cinder import exception
from cinder.volume.driver import VolumeDriver
from cinder.volume import volume_types
from cinder.context import get_admin_context
try:
    from oslo_log import log as logging
except ImportError:
    from cinder.openstack.common import log as logging

from lunrdriver.lunr.client import LunrClient, LunrError
from utils import initialize_connection


lunr_opts = [
    cfg.StrOpt('lunr_api_endpoint', default='http://127.0.0.1:8080/v1.0',
               help='Lunr API endpoint'),
]


LOG = logging.getLogger('cinder.volume.driver.lunr')


class LunrDriver(VolumeDriver):
    """Executes commands relating to Volumes."""

    def __init__(self, *args, **kwargs):
        super(LunrDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(lunr_opts)
        self.url = self.configuration.lunr_api_endpoint

    def _create_volume(self, volume, snapshot=None, source=None,
                       image_id=None):
        model_update = {}
        model_update_meta = {}
        affinity = None
        for meta in volume.get('volume_metadata', []):
            model_update_meta[meta.key] = meta.value
            # Translating terms, rack->group. Last one specified wins.
            if meta.key == 'different_node':
                affinity = "different_node:%s" % meta.value
            if meta.key == 'different_rack':
                affinity = "different_group:%s" % meta.value
        try:
            # Try to get the volume type name, else use the default volume type
            volume_type_name = volume['volume_type']['name']
        except (KeyError, TypeError):
            raise RuntimeError("Cinder failed to assign a volume type;"
                               " is 'CONF.default_volume_type' set?")

        params = {
            'name': volume['id'],
            'size': volume['size'],
            'volume_type_name': volume_type_name,
        }
        if snapshot:
            params['backup'] = snapshot['id']
        if source:
            params['source_volume'] = source['id']
        if image_id:
            params['image_id'] = image_id
        if affinity:
            params['affinity'] = affinity

        # Make the Rest Call
        client = LunrClient(self.url, volume, logger=LOG)
        resp = client.volumes.create(volume['id'], **params)

        if resp.body['size'] != volume['size']:
            model_update['size'] = resp.body['size']
        if resp.body.get('cinder_host'):
            model_update['host'] = resp.body['cinder_host']
        if resp.body.get('node_id'):
            model_update_meta['storage-node'] = resp.body['node_id']

        # return any model changes that cinder should make
        if model_update_meta:
            model_update['metadata'] = model_update_meta
        return model_update

    def create_volume(self, volume):
        """Call the Lunr API to request a volume """
        return self._create_volume(volume)

    def create_cloned_volume(self, volume, src_vref):
        """Call the Lunr API to request a clone """
        model_update = self._create_volume(volume, source=src_vref)

        # Wait until the volume is ACTIVE
        client = LunrClient(self.url, volume, logger=LOG)
        client.volumes.wait_on_status(volume['id'], 'ACTIVE')

        return model_update

    def create_volume_from_snapshot(self, volume, snapshot):
        """Call the Lunr API to request a snapshot"""
        model_update = self._create_volume(volume, snapshot=snapshot)

        # Wait until the snapshot is ACTIVE
        client = LunrClient(self.url, volume, logger=LOG)
        client.volumes.wait_on_status(volume['id'], 'ACTIVE')

        return model_update

    def clone_image(self, volume, image_location, image_id, image_meta):
        model_update = self._create_volume(volume, image_id=image_id)

        # Wait until the snapshot is ACTIVE
        client = LunrClient(self.url, volume, logger=LOG)
        client.volumes.wait_on_status(volume['id'], 'ACTIVE', 'IMAGING_SCRUB')

        return model_update, True

    def delete_volume(self, volume):
        try:
            client = LunrClient(self.url, volume, logger=LOG)
            client.volumes.delete(volume['id'])
        except LunrError, e:
            # ignore Not Found on delete
            if e.code != 404:
                raise

    def create_export(self, context, volume, connector=None):
        """Exports the volume. Can optionally return a Dictionary of changes
        to the volume object to be persisted."""
        pass

    def remove_export(self, context, volume):
        """Removes an export for a logical volume."""
        pass

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        client = LunrClient(self.url, snapshot, logger=LOG)
        params = {
            'volume': snapshot['volume_id']
        }
        client.backups.create(snapshot['id'], **params)
        client.backups.wait_on_status(snapshot['id'], 'AVAILABLE')

    def delete_snapshot(self, snapshot):
        client = LunrClient(self.url, snapshot, logger=LOG)
        try:
            client.backups.delete(snapshot['id'])
            client.backups.wait_on_status(snapshot['id'],
                                          'DELETED', 'AUDITING')
        except LunrError, e:
            # ignore Not Found on delete_snapshot. Don't wait on status.
            if e.code == 404:
                return
            raise

    def check_for_setup_error(self):
        """
        Runs once on startup of the manager, good a time as any to hit lunr and
        make sure cinder's got the types in the db.
        """
        lunr_admin_context = {'project_id': 'admin'}
        max_attempts = 3
        attempt = 0
        while True:
            attempt += 1
            try:
                client = LunrClient(self.url, lunr_admin_context, logger=LOG)
                resp = client.types.list()
            except Exception:
                if attempt >= max_attempts:
                    LOG.error('Unable up to read volume types from Lunr '
                              'after %s attempts.' % attempt)
                    return
                LOG.exception('failed attempt %s to retrieve volume types '
                              'from %s, will retry.' % (attempt, self.url))
                sleep(attempt ** 2)
            else:
                LOG.info('successfully pulled volume types from Lunr')
                break
        context = get_admin_context()
        for vtype in resp.body:
            if vtype['status'] != 'ACTIVE':
                LOG.debug('ignoring type %s with status %s' % (
                    vtype['name'], vtype['status']))
                continue
            try:
                volume_types.create(context, vtype['name'])
                LOG.info('volume type %s successfully created' % vtype['name'])
            except exception.VolumeTypeExists:
                LOG.info('volume type %s already exists' % vtype['name'])

    def ensure_export(self, context, volume):
        """Synchronously recreates an export for a logical volume."""
        # TODO: recreate export if needed?
        pass

    def initialize_connection(self, volume, connector, initiator_data=None):
        """Create export and return connection info."""
        client = LunrClient(self.url, volume, logger=LOG)
        return initialize_connection(client, volume['id'], connector)

    def terminate_connection(self, volume, connector, force=False):
        """Delete lunr export."""
        client = LunrClient(self.url, volume, logger=LOG)
        initiator = connector.get('initiator')
        client.exports.delete(volume['id'], force=force, initiator=initiator)

    def attach_volume(self, context, volume, instance_uuid, host_name,
                      mountpoint):
        """Update lunr export metadata."""
        client = LunrClient(self.url, volume, logger=LOG)
        return client.exports.update(volume['id'], instance_id=instance_uuid,
                                     mountpoint=mountpoint, status='ATTACHED')

    def detach_volume(self, context, volume, attachment=None):
        """Update lunr export metadata."""
        client = LunrClient(self.url, volume, logger=LOG)
        return client.exports.update(volume['id'], instance_id=None)

    def get_volume_stats(self, refresh=False):
        """
        This task is already hooked into the VolumeManager to run periodically,
        but if you return anything it does all this weird propriatary stuff.

        We can do whatever we want here, and don't return anything.
        """
        #TODO: look for volumes stuck in attaching?
        stats = {'driver_version': '0.0.12',
                 'free_capacity_gb': 'infinite',
                 'reserved_percentage': 0,
                 'storage_protocol': 'lunr',
                 'total_capacity_gb': 'infinite',
                 'vendor_name': 'Rackspace',
                 'volume_backend_name': 'lunr'
                }
        return stats
