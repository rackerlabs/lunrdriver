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


import socket

from lunrdriver.lunr.client import LunrClient


def resolve_hostname(target_portal):
    """
    Try to lookup the ip of the of the hostname in target_portal.

    Thin wrapper around the socket module's gethostbyname function.

    :param target_portal: a string, in the format "hostname:port"

    :returns: "host_ip:port" or "hostname:port" if lookup fails
    """
    hostname, port = target_portal.split(':')
    try:
        host_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        # this host can't resolve, just pass the name through
        return target_portal
    return ':'.join((host_ip, port))


def initialize_connection(client, volume_id):
    resp = client.exports.create(volume_id)
    if '.' in resp.body['target_portal']:
        target_portal = resp.body['target_portal']
    else:
        # iscsiadm has trouble with /etc/hosts entries
        target_portal = resolve_hostname(resp.body['target_portal'])
    return {
        'driver_volume_type': 'iscsi',
        'data': {
            'target_discovered': False,
            'target_iqn': resp.body['target_name'],
            'target_portal': target_portal,
            'volume_id': volume_id,
        }
    }


def terminate_connection(client, volume_id, force=False):
    client.exports.delete(volume_id, force=force)


def attach_volume(client, volume_id, instance_id, mountpoint):
    client.exports.update(volume_id, instance_id=instance_id,
                          mountpoint=mountpoint, status='ATTACHED')


def detach_volume(client, volume_id):
    client.exports.update(volume_id, instance_id=None)
