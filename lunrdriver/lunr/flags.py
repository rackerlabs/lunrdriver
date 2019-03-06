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
    from oslo_config import cfg
    from oslo_config.cfg import DuplicateOptError
except ImportError:
    from oslo.config import cfg
    from oslo.config.cfg import DuplicateOptError

lunr_opts = [
    cfg.StrOpt('lunr_api_endpoint', default='http://127.0.0.1:8080/v1.0',
               help='Lunr API endpoint'),
    cfg.ListOpt('lunr_volume_types', default=[],
               help='Types belonging to Lunr'),
    cfg.BoolOpt('lunr_volume_clone_enabled', default=True,
                help='Disable create from source.'),
    cfg.BoolOpt('lunr_copy_image_enabled', default=True,
                help='Disable create from image.'),
]

CONF = cfg.CONF
CONF.register_opts(lunr_opts)

