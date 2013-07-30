# Copyright (c) 2012 OpenStack Foundation.
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

import contextlib
import logging
import mock

from quantum.common import config
from quantum import context
from quantum.db import api as db
from quantum.manager import QuantumManager
from quantum.plugins.cisco.common import cisco_constants as const
from quantum.plugins.cisco.common import cisco_credentials_v2
from quantum.plugins.cisco.db import network_db_v2
from quantum.plugins.cisco.db import network_models_v2
from quantum.plugins.cisco import l2network_plugin_configuration
from quantum.plugins.cisco.models import virt_phy_sw_v2
from quantum.plugins.cisco.nexus import cisco_nexus_configuration
from quantum.plugins.openvswitch.common import config as ovs_config
from quantum.tests.unit import test_db_plugin

LOG = logging.getLogger(__name__)
DEVICE_ID_1 = '11111111-1111-1111-1111-111111111111'
DEVICE_ID_2 = '22222222-2222-2222-2222-222222222222'


class CiscoNetworkPluginV2TestCase(test_db_plugin.QuantumDbPluginV2TestCase):

    _plugin_name = 'quantum.plugins.cisco.network_plugin.PluginV2'
    mock_ncclient = mock.Mock()

    def setUp(self):
        self.addCleanup(mock.patch.stopall)

        # Use a mock netconf client
        ncclient_patch = {
            'ncclient': CiscoNetworkPluginV2TestCase.mock_ncclient
        }
        mock.patch.dict('sys.modules', ncclient_patch).start()

        super(CiscoNetworkPluginV2TestCase, self).setUp(self._plugin_name)
        self.port_create_status = 'DOWN'

    def _get_plugin_ref(self):
        plugin_obj = QuantumManager.get_plugin()
        if getattr(plugin_obj, "_master"):
            plugin_ref = plugin_obj
        else:
            plugin_ref = getattr(plugin_obj, "_model").\
                _plugins[const.VSWITCH_PLUGIN]

        return plugin_ref


class TestCiscoBasicGet(CiscoNetworkPluginV2TestCase,
                        test_db_plugin.TestBasicGet):
    pass


class TestCiscoV2HTTPResponse(CiscoNetworkPluginV2TestCase,
                              test_db_plugin.TestV2HTTPResponse):

    pass


class TestCiscoPortsV2(CiscoNetworkPluginV2TestCase,
                       test_db_plugin.TestPortsV2):

    def setUp(self):
        """Configure for end-to-end quantum testing using a mock ncclient.

        This setup includes:
        - Configure the OVS plugin to use VLANs in the range of 1000-1100.
        - Configure the Cisco plugin model to use the real Nexus driver.
        - Configure the Nexus sub-plugin to use an imaginary switch
          at 1.1.1.1.

        """
        self.addCleanup(mock.patch.stopall)

        ovs_opts = {
            'bridge_mappings': 'physnet1:br-eth1',
            'network_vlan_ranges': ['physnet1:1000:1100'],
            'tenant_network_type': 'vlan',
        }
        for opt in ovs_opts:
            ovs_config.cfg.CONF.set_override(opt, ovs_opts[opt], 'OVS')
        self.addCleanup(ovs_config.cfg.CONF.reset)

        vswitch_plugin = ('quantum.plugins.openvswitch.'
                          'ovs_quantum_plugin.OVSQuantumPluginV2')
        nexus_plugin = ('quantum.plugins.cisco.nexus.'
                        'cisco_nexus_plugin_v2.NexusPlugin')
        nexus_driver = ('quantum.plugins.cisco.nexus.'
                        'cisco_nexus_network_driver_v2.CiscoNEXUSDriver')
        switch_ip = '1.1.1.1'
        nexus_config = {
            switch_ip: {
                'testhost': {'ports': '1/1'},
                'ssh_port': {'ssh_port': 22},
            }
        }
        nexus_creds = {
            switch_ip: {
                'username': 'admin',
                'password': 'mySecretPassword',
            }
        }
        mock.patch.dict(l2network_plugin_configuration.PLUGINS['PLUGINS'],
                        {
                            'vswitch_plugin': vswitch_plugin,
                            'nexus_plugin': nexus_plugin,
                        }).start()
        mock.patch.object(cisco_nexus_configuration, 'NEXUS_DRIVER',
                          new=nexus_driver).start()
        mock.patch.dict(cisco_nexus_configuration.CP['SWITCH'],
                        nexus_config).start()
        mock.patch.dict(cisco_credentials_v2._creds_dictionary,
                        nexus_creds).start()

        mock_sw = mock.patch.object(
            virt_phy_sw_v2.VirtualPhysicalSwitchModelV2,
            '_get_instance_host').start()
        mock_sw.return_value = 'testhost'

        super(TestCiscoPortsV2, self).setUp()

    @contextlib.contextmanager
    def _create_port_res(self, name='myname', cidr='1.0.0.0/24',
                         device_id=DEVICE_ID_1, do_delete=True):
        """Create a network, subnet, and port and yield the result.

        Create a network, subnet, and port, yield the result,
        then delete the port, subnet, and network.

        :param name: Name of network to be created
        :param cidr: cidr address of subnetwork to be created
        :param device_id: Device ID to use for port to be created
        :param do_delete: If set to True, delete the port at the
                          end of testing

        """
        with self.network(name=name) as network:
            with self.subnet(network=network, cidr=cidr) as subnet:
                net_id = subnet['subnet']['network_id']
                res = self._create_port(self.fmt, net_id, device_id=device_id)
                port = self.deserialize(self.fmt, res)
                try:
                    yield res
                finally:
                    if do_delete:
                        self._delete('ports', port['port']['id'])

    def _is_in_last_nexus_cfg(self, words):
        last_cfg = (CiscoNetworkPluginV2TestCase.mock_ncclient.manager.
                    connect.return_value.edit_config.
                    mock_calls[-1][2]['config'])
        return all(word in last_cfg for word in words)

    def test_create_ports_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_port
            with mock.patch.object(plugin_ref,
                                   'create_port') as patched_plugin:

                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)

                patched_plugin.side_effect = side_effect
                with self.network() as net:
                    res = self._create_port_bulk(self.fmt, 2,
                                                 net['network']['id'],
                                                 'test',
                                                 True)
                    # We expect a 500 as we injected a fault in the plugin
                    self._validate_behavior_on_bulk_failure(res, 'ports', 500)

    def test_create_ports_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk port create")
        ctx = context.get_admin_context()
        with self.network() as net:
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_port
            with mock.patch.object(plugin_ref,
                                   'create_port') as patched_plugin:

                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)

                patched_plugin.side_effect = side_effect
                res = self._create_port_bulk(self.fmt, 2, net['network']['id'],
                                             'test', True, context=ctx)
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'ports', 500)

    def test_nexus_enable_vlan_cmd(self):
        """Verify the syntax of the command to enable a vlan on an intf."""
        # First vlan should be configured without 'add' keyword
        with self._create_port_res(name='net1', cidr='1.0.0.0/24',
                                   device_id=DEVICE_ID_1):
            self.assertTrue(self._is_in_last_nexus_cfg(['allowed', 'vlan']))
            self.assertFalse(self._is_in_last_nexus_cfg(['add']))
            # Second vlan should be configured with 'add' keyword
            with self._create_port_res(name='net2', cidr='1.0.1.0/24',
                                       device_id=DEVICE_ID_2):
                self.assertTrue(
                    self._is_in_last_nexus_cfg(['allowed', 'vlan', 'add']))


class TestCiscoNetworksV2(CiscoNetworkPluginV2TestCase,
                          test_db_plugin.TestNetworksV2):

    def test_create_networks_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_network
        #ensures the API choose the emulation code path
        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            with mock.patch.object(plugin_ref,
                                   'create_network') as patched_plugin:
                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)
                patched_plugin.side_effect = side_effect
                res = self._create_network_bulk(self.fmt, 2, 'test', True)
                LOG.debug("response is %s" % res)
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'networks', 500)

    def test_create_networks_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk network create")
        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_network
        with mock.patch.object(plugin_ref,
                               'create_network') as patched_plugin:

            def side_effect(*args, **kwargs):
                return self._do_side_effect(patched_plugin, orig,
                                            *args, **kwargs)

            patched_plugin.side_effect = side_effect
            res = self._create_network_bulk(self.fmt, 2, 'test', True)
            # We expect a 500 as we injected a fault in the plugin
            self._validate_behavior_on_bulk_failure(res, 'networks', 500)


class TestCiscoSubnetsV2(CiscoNetworkPluginV2TestCase,
                         test_db_plugin.TestSubnetsV2):

    def test_create_subnets_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_subnet
            with mock.patch.object(plugin_ref,
                                   'create_subnet') as patched_plugin:

                def side_effect(*args, **kwargs):
                    self._do_side_effect(patched_plugin, orig,
                                         *args, **kwargs)

                patched_plugin.side_effect = side_effect
                with self.network() as net:
                    res = self._create_subnet_bulk(self.fmt, 2,
                                                   net['network']['id'],
                                                   'test')
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'subnets', 500)

    def test_create_subnets_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk subnet create")
        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_subnet
        with mock.patch.object(plugin_ref,
                               'create_subnet') as patched_plugin:
            def side_effect(*args, **kwargs):
                return self._do_side_effect(patched_plugin, orig,
                                            *args, **kwargs)

            patched_plugin.side_effect = side_effect
            with self.network() as net:
                res = self._create_subnet_bulk(self.fmt, 2,
                                               net['network']['id'],
                                               'test')

                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'subnets', 500)


class TestCiscoPortsV2XML(TestCiscoPortsV2):
    fmt = 'xml'


class TestCiscoNetworksV2XML(TestCiscoNetworksV2):
    fmt = 'xml'


class TestCiscoSubnetsV2XML(TestCiscoSubnetsV2):
    fmt = 'xml'