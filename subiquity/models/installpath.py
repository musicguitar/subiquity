# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging


log = logging.getLogger("subiquity.models.installpath")


class InstallpathModel(object):
    """ Model representing install options

    List of install paths in the form of:
    ('UI Text seen by user', <signal name>, <callback function string>)
    """

    def _refresh_install_paths(self):
        # TODO: Re-enable once available
        self.install_paths = [
            (_('Install Ubuntu'),             'installpath:install-ubuntu'),
            # ('Install MAAS Region Server',  'installpath:maas-region-server'),
            # ('Install MAAS Cluster Server', 'installpath:maas-cluster-server'),
            # ('Test installation media',     'installpath:test-media'),
            # ('Test machine memory',         'installpath:test-memory')
        ]

    def get_menu(self):
        self._refresh_install_paths()
        return self.install_paths
