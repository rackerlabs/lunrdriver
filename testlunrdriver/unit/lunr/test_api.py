import os
import shutil
import tempfile

import mock
from oslo_config import cfg
from oslo_utils import importutils

from stevedore import extension

from cinder.brick.local_dev import lvm as brick_lvm
from cinder import context

from cinder import test
from cinder.tests.unit.image import fake as fake_image
from cinder.tests.unit import utils as tests_utils
import cinder.volume
from cinder.volume import configuration as conf

import lunrdriver.lunr
from lunrdriver.lunr import api as volume_api
from lunrdriver.lunr.api import SnapshotQuotaExceedConflict


CONF = cfg.CONF


class BaseVolumeTestCase(test.TestCase):
	"""Test Case for volumes."""

	FAKE_UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa'

	def setUp(self):
	    super(BaseVolumeTestCase, self).setUp()
	    self.extension_manager = extension.ExtensionManager(
	        "BaseVolumeTestCase")
	    vol_tmpdir = tempfile.mkdtemp()
	    self.flags(volumes_dir=vol_tmpdir,
	               notification_driver=["test"])
	    self.addCleanup(self._cleanup)
	    self.volume = importutils.import_object(CONF.volume_manager)
	    self.configuration = mock.Mock(conf.Configuration)
	    self.context = context.get_admin_context()
	    self.context.user_id = 'fake'
	    # NOTE(mriedem): The id is hard-coded here for tracking race fail
	    # assertions with the notification code, it's part of an
	    # elastic-recheck query so don't remove it or change it.
	    self.project_id = '7f265bd4-3a85-465e-a899-5dc4854a86d3'
	    self.context.project_id = self.project_id
	    self.volume_params = {
	        'status': 'creating',
	        'host': CONF.host,
	        'size': 1}
	    self.stubs.Set(brick_lvm.LVM,
	                   'get_all_volume_groups',
	                   self.fake_get_all_volume_groups)
	    fake_image.stub_out_image_service(self.stubs)
	    self.stubs.Set(brick_lvm.LVM, '_vg_exists', lambda x: True)
	    self.stubs.Set(os.path, 'exists', lambda x: True)
	    self.volume.driver.set_initialized()
	    self.volume.stats = {'allocated_capacity_gb': 0,
	                         'pools': {}}
	    # keep ordered record of what we execute
	    self.called = []

	def _cleanup(self):
	    try:
	        shutil.rmtree(CONF.volumes_dir)
	    except OSError:
	        pass

	def fake_get_all_volume_groups(obj, vg_name=None, no_suffix=True):
	    return [{'name': 'cinder-volumes',
	             'size': '5.00',
	             'available': '2.50',
	             'lv_count': '2',
	             'uuid': 'vR1JU3-FAKE-C4A9-PQFh-Mctm-9FwA-Xwzc1m'}]


class VolumeTest(BaseVolumeTestCase):
	
	def setUp(self):
	    super(VolumeTest, self).setUp()
	    self._clear_patch = mock.patch('cinder.volume.utils.clear_volume',
	                                   autospec=True)
	    self._clear_patch.start()
	    self.expected_status = 'available'

	def tearDown(self):
	    super(VolumeTest, self).tearDown()
	    self._clear_patch.stop()


	def test_create_snapshot_limit(self):
		# Creating Mock Volume
		instance_uuid = '12345678-1234-5678-1234-567812345678'
		volume = tests_utils.create_volume(self.context, **self.volume_params)
		self.volume.create_volume(self.context, volume['id'])
		# changing the value of snapshots quota in config
		quota_snapshots = CONF.quota_snapshots
		CONF.quota_snapshots = 1
		# Feetching Volume Data
		volume_api = lunrdriver.lunr.api.API()
		volume = volume_api.get(self.context, volume['id'])
		# create snapshot 1
		snapshot_ref1 = volume_api.create_snapshot(self.context,
		                                        volume,
		                                        'fake_name',
		                                        'fake_description')
		# create snapshot 2
		self.assertEquals("creating", str(snapshot_ref1.status))

		self.assertRaises(SnapshotQuotaExceedConflict, volume_api.create_snapshot,
						  self.context, volume, 'fake_name2', 'fake_description2')

		# test tear down activities

		snapshot_ref1.destroy()
		CONF.quota_snapshots = quota_snapshots


if __name__ == "__main__":
	unittest.main()