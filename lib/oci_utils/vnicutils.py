# oci-utils
#
# Copyright (c) 2018, 2021 Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown
# at http://oss.oracle.com/licenses/upl.

import logging
import os
import os.path
from .oci_api import OCISession
from . import cache
from .metadata import InstanceMetadata
from .impl import network_helpers as NetworkHelpers
from .impl.network_interface import NetworkInterfaceSetupHelper, _intf_dict
from .impl import sudo_utils

_logger = logging.getLogger('oci-utils.vnicutils')


class VNICUtils:
    """Class for managing VNICs
    """
    # file with saved vnic information
    __vnic_info_file = "/var/lib/oci-utils/vnic_info"
    # OBSOLETE: file with VNICs and stuff to exclude from automatic
    # configuration. only kept for migration
    __net_exclude_file = "/var/lib/oci-utils/net_exclude"

    def __init__(self):
        """ Class VNICUtils initialisation.
        """
        self.vnic_info = self.get_vnic_info()
        _logger.debug('_GT_ vnic_info %s', self.vnic_info)
        self._metadata = None
        try:
            self._metadata = InstanceMetadata().refresh()
        except IOError as e:
            _logger.warning('Cannot get metadata: %s', str(e))

    @staticmethod
    def __new_vnic_info():
        """
        Create a new vnic info file

        Returns
        -------
        tuple
            (vnic info timestamp: datetime, vnic info: dict)
        """
        _vnic_info = {
            'exclude': []}

        # migration from oci-utils 0.5's net_exclude file
        excludes = cache.load_cache(VNICUtils.__net_exclude_file)[1]
        if excludes is not None:
            _vnic_info['exclude'] = excludes
            cache.write_cache(cache_content=_vnic_info,
                              cache_fname=VNICUtils.__vnic_info_file)
            try:
                os.remove(VNICUtils.__net_exclude_file)
            except Exception as e:
                _logger.debug('Cannot remove file [%s]: %s', VNICUtils.__net_exclude_file, str(e))

            _logger.debug('Excluded intf: %s ', excludes)

        return _vnic_info

    def get_excluded_interfaces(self):
        """
        gets excluded interface from auto configuration/deconfiguration
        """
        return self.vnic_info['exclude']

    def get_vnic_info(self):
        """
        Load the vnic_info file. If the file is missing , a new one is created.

        Returns
        -------
        tuple (int, dict)
            (vnic info timestamp: datetime, vnic info: dict)
        """
        self.vnic_info_ts, self.vnic_info = cache.load_cache(VNICUtils.__vnic_info_file)
        if self.vnic_info is None:
            self.vnic_info = {'exclude': []}

        return self.vnic_info

    def save_vnic_info(self):
        """
        Save self.vnic_info in the vnic_info file.

        Returns
        -------
        int
            The timestamp of the file or None on failure.
        """
        _logger.debug("Saving vnic_info.")
        _ = cache.write_cache(cache_content=self.vnic_info,
                              cache_fname=VNICUtils.__vnic_info_file)

    def set_namespace(self, ns):
        """
        Set the 'ns' field of the vnic_info dict to the given value. This
        value is passed to the secondary vnic script with the -n option and
        is used to place the interface in the given namespace. The default
        is no namespace.

        Parameters
        ----------
        ns: str
            The namespace value.
        """
        self.vnic_info['ns'] = ns

    def set_sshd(self, val):
        """
        Set the 'sshd' field of the vnic_info dict to the given value.

        Parameters
        ----------
        val: bool
            When set to True, the secondary vnic script is called with
            the -r option, which, if a namespace is also specified,
            runs sshd in the namespace. The default is False.
        """
        self.vnic_info['sshd'] = val

    def add_private_ip(self, ipaddr, vnic_id):
        """
        Add the given secondary private IP to vnic_info save vnic info to
        the vnic_info file.

        Parameters
        ----------
        ipaddr: str
            The secondary IP address.
        vnic_id: int
            The VNIC id.
        """
        _interfaces = self.get_network_config()
        _intf = None
        for _interface in _interfaces:
            if _interface.get('VNIC') == vnic_id:
                _intf = _interface
                break
        if _intf is None:
            # cannot happen
            _logger.debug('WARNING : cannot find vnic with id [%s]: caller did not check ?')
        if 'MISSING_SECONDARY_IPS' not in _intf:
            _intf['MISSING_SECONDARY_IPS'] = [ipaddr]
        else:
            if ipaddr not in _intf['MISSING_SECONDARY_IPS']:
                _intf['MISSING_SECONDARY_IPS'].append(ipaddr)

        self._config_secondary_intf(_intf)

    def del_private_ip(self, ipaddr, vnic_id):
        """
        Delete secondary private IP from vnic_info save vnic_info to the
        vnic_info file.

        Parameters
        ----------
        ipaddr: str
            The IP addr to be removed.
        vnic_id: int
            The VNIC ID.

        Returns
        -------
        tuple
            (exit code: int, output message).
        """

        _interfaces = self.get_network_config()
        _interface_to_delete = None
        for _interface in _interfaces:
            if _interface.get('VNIC') == vnic_id \
                    and (_interface.get('ADDR') == ipaddr
                         or ipaddr in _interface.get('SECONDARY_ADDRS', ())):
                _interface_to_delete = _interface
                break

        if not _interface_to_delete:
            _logger.debug('del_private_ip. IP [%s] not found on vNIC %s', ipaddr, vnic_id)
            return 0, 'IP %s is not configured.' % ipaddr

        # 1. delete any rule for this ip
        NetworkHelpers.remove_ip_addr_rules(ipaddr)

        # 2. remove addr from the system
        NetworkInterfaceSetupHelper(_interface_to_delete).remove_secondary_address(ipaddr)

        # 3. removes the mac address from the unmanaged-devices list in then NetworkManager.conf file.
        NetworkHelpers.add_mac_to_nm(_interface_to_delete['MAC'])

        return 0, ''

    def _is_intf_excluded(self, interface):
        """
        Checks if this interface is excluded
        Checks if interface name, VNIC ocid or ip addr is part of excluded items
        """

        for excl in self.vnic_info.get('exclude', ()):
            if excl in (interface['IFACE'], interface['VNIC'], interface['ADDR']):
                return True
        return False

    def exclude(self, item):
        """
        Remove item from the "exclude" list. IP addresses or interfaces that are
        excluded from automatic configuration.

        Parameters
        ----------
        item: str
            Item (IP or interface) to be excluded.
        """
        if item not in self.vnic_info['exclude']:
            _logger.debug('Adding %s to "exclude" list', item)
            self.vnic_info['exclude'].append(item)
            self.save_vnic_info()

    def include(self, item):
        """
        Add item to the "exclude" list, IP addresses or interfaces that
        are excluded from automatic configuration.

        Parameters
        ----------
        item: str
            Item (IP or interface) to be excluded.
        """
        if item in self.vnic_info['exclude']:
            _logger.debug('Removing %s from "exclude" list', item)
            self.vnic_info['exclude'].remove(item)
            self.save_vnic_info()

    def auto_config(self, sec_ip):
        """
        Auto configure VNICs.

        Parameters
        ----------
        sec_ip: list of tuple (<ip adress>,<vnic ocid>)
            secondary IPs to ad to vnics. can be None or empty

        Returns
        -------

        """

        _all_intf = self.get_network_config()

        # we may need a mapping of intf by physical NIC index
        # for BMs secondary VNIC are not plumbed
        # {<index>: <intf name>}
        _by_nic_index = {}

        # the interfaces to be configured according to metadata
        _all_to_be_configured = []
        # the interfaces on which a secondary interface must be added
        _all_to_be_modified = []
        # the interfaces to be unconfigured according to metadata
        _all_to_be_deconfigured = []

        # 1.1 compose list of interface which need configuration
        # 1.2 compose list of interface which need deconfiguration
        for _intf in _all_intf:

            if _intf['IFACE'] != '-':
                # keep track of interface by NIC index
                _by_nic_index[_intf['NIC_I']] = _intf['IFACE']

            # Is this intf excluded ?
            if self._is_intf_excluded(_intf):
                continue

            # add secondary IPs if any
            if sec_ip:
                for (ip, vnic) in sec_ip:
                    if vnic == _intf['VNIC']:
                        if 'MISSING_SECONDARY_IPS' not in _intf:
                            _intf['MISSING_SECONDARY_IPS'] = [ip]
                        else:
                            if ip not in _intf['MISSING_SECONDARY_IPS']:
                                _intf['MISSING_SECONDARY_IPS'].append(ip)

            if _intf['CONFSTATE'] == 'ADD':
                _all_to_be_configured.append(_intf)
                # take care of secondary addresses.
                # at this point we cannot rely on MISSING_SECONDARY_IPS as we are configured "new" interface
                # in order to use the same code path, set MISSING_SECONDARY_IPS here so _all_to_be_modified set
                # will also contain this one. Need better refactoring: enough for now.
                if len(_intf.get('SECONDARY_ADDRS', ())) > 0:
                    _intf['MISSING_SECONDARY_IPS'] = _intf['SECONDARY_ADDRS']
            if _intf['CONFSTATE'] == 'DELETE':
                _all_to_be_deconfigured.append(_intf)
            if 'MISSING_SECONDARY_IPS' in _intf:
                _all_to_be_modified.append(_intf)

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug("interfaces to be configured: %d", len(_all_to_be_configured))
            for _in in _all_to_be_configured:
                _logger.debug("CONFIGURE %s", _in)
            _logger.debug("interfaces to be unconfigured: %d", len(_all_to_be_deconfigured))
            for _in in _all_to_be_deconfigured:
                _logger.debug("DECONFIGURE %s", _in)
            _logger.debug("interfaces to be modified: %d", len(_all_to_be_modified))
            for _in in _all_to_be_modified:
                _logger.debug("MODIFY %s", _in)

        # 2 configure the one which need it
        for _intf in _all_to_be_configured:
            ns_i = None
            if 'ns' in self.vnic_info:
                # if requested to use namespace, compute namespace name pattern
                ns_i = {}
                if self.vnic_info['ns']:
                    ns_i['name'] = self.vnic_info['ns']
                else:
                    ns_i['name'] = 'ons%s' % _intf['IFACE']

                ns_i['start_sshd'] = 'sshd' in self.vnic_info
            try:
                # for BMs, IFACE can be empty ('-'), we local physical NIC
                # thank to NIC index
                # make a copy of it to change the IFACE
                _intf_to_use = _intf_dict(_intf)

                if self._metadata is None:
                    raise ValueError('no metadata information')

                if self._metadata['instance']['shape'].startswith('BM') and _intf['IFACE'] == '-':
                    _intf_to_use['IFACE'] = _by_nic_index[_intf['NIC_I']]
                    _intf_to_use['STATE'] = "up"

                if _logger.isEnabledFor(logging.DEBUG):
                    _logger.debug("begin configuration of %s", _intf_to_use)

                _auto_config_intf(ns_i, _intf_to_use)

                # disable network manager for that device
                NetworkHelpers.remove_mac_from_nm(_intf['MAC'])

                # setup routes
                self._auto_config_intf_routing(ns_i, _intf_to_use)

            except Exception as e:
                # best effort , just issue warning
                _logger.warning('Cannot configure %s: %s', _intf_to_use, str(e))

        # 3 deconfigure the one which need it
        for _intf in _all_to_be_deconfigured:
            try:
                self._auto_deconfig_intf_routing(_intf)
                _auto_deconfig_intf(_intf)
            except Exception as e:
                # best effort , just issue warning
                _logger.warning('Cannot deconfigure %s: %s', _intf, str(e))

        # 4 add secondaries IP address
        for _intf in _all_to_be_modified:
            if self._metadata['instance']['shape'].startswith('BM') and _intf['IFACE'] == '-':
                # it may happen if we came after configuring the interface by injecting MISSING_SECONDARY_IPS
                _intf['IFACE'] = _by_nic_index[_intf['NIC_I']]
                _intf['STATE'] = "up"
            self._config_secondary_intf(_intf)

    def _deconfig_secondary_addr(self, intf_infos, address):
        """
        Removes an IP address from a device

        Parameters:
        -----------
            device: network device as str
            address: IP address to be removed
            namespace: the network namespace (optional)
        Returns:
        --------
          None
        Raise:
        ------
            Exception in case of failure
        """
        _logger.debug("Removing IP addr rules")
        NetworkHelpers.remove_ip_addr_rules(address)
        _logger.debug("Removing IP addr [%s] from [%s]", address, intf_infos)
        NetworkInterfaceSetupHelper(intf_infos).remove_secondary_address(address)

    def auto_deconfig(self, sec_ip):
        """
        De-configure VNICs. Run the secondary vnic script in automatic
        de-configuration mode (-d).

        Parameters
        ----------
        sec_ip: list of tuple (<ip adress>,<vnic ocid>)
            secondary IPs to ad to vnics. can be None or empty
        Returns
        -------
        tuple
            (exit code: int, output from the "sec vnic" script execution.)
        """

        _all_intf = self.get_network_config()

        # if we have secondary addrs specified, just take care of these
        #  vnic OCID give us the mac address then select the right interface which has the ip
        if sec_ip:
            _translated = []
            if self._metadata is None:
                return 1, 'no metadata available'
            _all_vnic_md = self._metadata['vnics']
            # 1. locate the MAC: translate ip/vnic to ip/mac
            for (ip, vnic) in sec_ip:
                _found = False
                for md_vnic in _all_vnic_md:
                    if md_vnic['vnicId'] == vnic:
                        _found = True
                        _logger.debug('located vnic, mac is %s', md_vnic['macAddr'])
                        _translated.append((ip, md_vnic['macAddr']))
                        break
                if not _found:
                    _logger.warning('VNIC not found : %s ', vnic)

            for (ip, mac) in _translated:
                # fecth right intf
                _found = False
                for intf in _all_intf:
                    if intf['MAC'] == mac:
                        if 'SECONDARY_ADDRS' in intf and ip in intf['SECONDARY_ADDRS']:
                            _found = True
                            self._deconfig_secondary_addr(intf, ip)
                            break
                if not _found:
                    _logger.warning('IP %s not found', ip)

        else:
            # unconfigure all
            for intf in _all_intf:
                # Is this intf the primary  ?
                if intf.has('IS_PRIMARY'):
                    continue
                # Is this intf has a configuration to be removed ?
                if intf['CONFSTATE'] == 'ADD':
                    continue
                # Is this intf excluded ?
                if self._is_intf_excluded(intf):
                    continue
                for secondary_addr in intf.get('SECONDARY_ADDRS', ()):
                    self._deconfig_secondary_addr(intf, secondary_addr)
                self._auto_deconfig_intf_routing(intf)
                _auto_deconfig_intf(intf)

        return 0, ''

    def _get_priv_addrs(self):
        """
        Gets all vnic private addrs
        returns:
        --------
          dict : a vnic ocid indexed dict of list of IPs
        """
        res = {}
        oci_sess = None
        try:
            oci_sess = OCISession()
        except Exception as e:
            _logger.debug('Cannot get OCI session: %s', str(e))

        p_ips = oci_sess.this_instance().all_private_ips()
        for p_ip in p_ips:
            _ocid = p_ip.get_vnic_ocid()
            _addr = p_ip.get_address()
            if _ocid not in res:
                res[_ocid] = []
            res[_ocid].append(_addr)

        return res

    def get_network_config(self):
        """
        Get network configuration.
        fetch information from this instance metadata and aggregate
        it to system information. Information form metadata take precedence

        Returns
        -------
        list of dict
           keys are
            CONFSTATE  'uncfg' indicates missing IP config, 'missing' missing VNIC,
                            'excl' excluded (-X), '-' hist configuration match oci vcn configuration
            ADDR       IP address
            SPREFIX    subnet CIDR prefix
            SBITS      subnet mask bits
            VIRTRT     virutal router IP address
            NS         namespace (if any)
            IND        interface index (if BM)
            IFACE      interface (underlying physical if VLAN is also set)
            VLTAG      VLAN tag (if BM)
            VLAN       IP virtual LAN (if any)
            STATE      state of interface
            MAC        MAC address
            NIC_I      (physical) NIC index
            VNIC       VNIC object identifier
            IS_PRIMARY is this interface the primary one ? (can be missing)
            SECONDARY_ADDRS secondary addresses
        """
        interfaces = []

        _all_intfs = NetworkHelpers.get_network_namespace_infos()

        # for BM cases (using macvlan/vlan) when using namespace , some interfaces (the macvlan ones within namespace)
        # do not have the 'link' property but the 'link_idx'
        # First build a "link by id" map
        # Note: loopback appears with index '1' in all namespaces.
        _link_by_idx = {}
        for _namespace, _nintfs in _all_intfs.items():
            for _i in _nintfs:
                _link_by_idx[_i['index']] = _i['device']

        _all_from_system = []
        for _namespace, _nintfs in _all_intfs.items():
            for _i in _nintfs:
                if "NO-CARRIER" in _i['flags'] or "LOOPBACK" in _i['flags']:
                    continue
                if _i['type'] != 'ether':
                    continue
                _intf = _intf_dict()
                if _i.get('mac'):
                    _intf['MAC'] = _i.get('mac')
                _intf['IFACE'] = _i['device']
                if 'link' in _i and _i['link'] is not None:
                    _intf['LINK'] = _i['link']
                else:
                    # in that case, try with index if we have it
                    if _i['link_idx']:
                        _intf['LINK'] = _link_by_idx[_i['link_idx']]
                if 'subtype' in _i:
                    _intf['LINKTYPE'] = _i['subtype']
                else:
                    _intf['LINKTYPE'] = 'ether'
                _intf['IND'] = _i['index']
                _intf['STATE'] = _i['opstate']
                # default namespace is empty string
                if _namespace and _namespace != '':
                    _intf['NS'] = _namespace
                if _i.get('vlanid'):
                    _intf['VLAN'] = _i.get('vlanid')
                if len(_i.get('addresses', [])) > 0:
                    _intf['CONFSTATE'] = '-'
                    _intf['ADDR'] = _i.get('addresses')[0]['address']
                    if len(_i.get('addresses', [])) > 1:
                        # first one in the list is the primary address of that vnic
                        _intf['SECONDARY_ADDRS'] = [ip['address'] for ip in _i.get('addresses')[1:]]
                else:
                    if not _i.get('is_vf'):
                        # by default, before correlation, set it to DELETE
                        _intf['CONFSTATE'] = 'DELETE'

                _all_from_system.append(_intf)

        _all_from_metadata = []
        _first_loop = True
        if self._metadata is None:
            _logger.warning('no metadata available')
        else:
            _ip_per_id = self._get_priv_addrs()

            for md_vnic in self._metadata['vnics']:
                _intf = _intf_dict()
                if _first_loop:
                    # primary always come first
                    _intf['IS_PRIMARY'] = True
                    _first_loop = False
                _intf['MAC'] = md_vnic['macAddr'].upper()
                _intf['ADDR'] = md_vnic['privateIp']
                _intf['SPREFIX'] = md_vnic['subnetCidrBlock'].split('/')[0]
                _intf['SBITS'] = md_vnic['subnetCidrBlock'].split('/')[1]
                _intf['VIRTRT'] = md_vnic['virtualRouterIp']
                _intf['VLTAG'] = md_vnic['vlanTag']
                _intf['VNIC'] = md_vnic['vnicId']
                if 'nicIndex' in md_vnic:
                    # VMs do not have such attr
                    _intf['NIC_I'] = md_vnic['nicIndex']
                if md_vnic['vnicId'] in _ip_per_id:
                    # get all but the primary one
                    _intf['SECONDARY_ADDRS'] = \
                        [_ip for _ip in _ip_per_id[md_vnic['vnicId']] if _ip != md_vnic['privateIp']]

                _all_from_metadata.append(_intf)

        # now we correlate informations
        # precedence is given to metadata
        for interface in _all_from_metadata:
            try:
                # locate the one with same ether address
                _candidates = [_i for _i in _all_from_system if _i['MAC'] == interface['MAC']]
                _state = 'ADD'
                _have_to_be_added = set()
                if len(_candidates) == 1:
                    # only one found , no ambiguity
                    # treat secondary addrs: if have some in metadata not present on system , we have to plumb them
                    _have_to_be_added = set(interface.get('SECONDARY_ADDRS', [])).difference(
                        _candidates[0].get('SECONDARY_ADDRS', []))
                    interface.update(_candidates[0])
                    if _candidates[0].has('ADDR'):
                        # an addr on the correlated system intf -> state is '-'
                        _state = '-'
                elif len(_candidates) >= 2:
                    # we do not expect to have more than 2 anyway
                    # surely macvlan/vlans involved (BM case)
                    #  the macvlan interface give us the addr and the actual link
                    #  the vlan interface give us the vlan name
                    _macvlan_is = [_i for _i in _candidates if _i['LINKTYPE'] in ('macvlan', 'macvtap')]
                    _vlan_is = [_i for _i in _candidates if _i['LINKTYPE'] == 'vlan']
                    if len(_macvlan_is) > 0 and len(_vlan_is) > 0:
                        # treat secondary addrs: if have some in metadata not present on system , we have to plumb them
                        _have_to_be_added = set(interface.get('SECONDARY_ADDRS', [])).difference(
                            _vlan_is[0].get('SECONDARY_ADDRS', []))
                        interface.update(_macvlan_is[0])
                        interface['VLAN'] = _vlan_is[0]['IFACE']
                        interface['IFACE'] = _macvlan_is[0]['LINK']
                        if _vlan_is[0].has('ADDR'):
                            _state = '-'
                        if _vlan_is[0].has('SECONDARY_ADDRS'):
                            interface['SECONDARY_ADDRS'] = _vlan_is[0]['SECONDARY_ADDRS']
                interface['CONFSTATE'] = _state

                # clean up system list
                _all_from_system = [_i for _i in _all_from_system if _i['MAC'] != interface['MAC']]
            except ValueError as e:
                _logger.debug('error while parsing [%s]: %s', str(interface), str(e))
            finally:
                if len(_have_to_be_added) > 0:
                    # this key will trigger configuration (see auto_config())
                    interface['MISSING_SECONDARY_IPS'] = list(_have_to_be_added)
                interfaces.append(interface)

        # now collect the one left omr system
        for interface in _all_from_system:
            interface['CONFSTATE'] = 'DELETE'
            interfaces.append(interface)

        # final round for the excluded
        for interface in interfaces:
            if self._is_intf_excluded(interface):
                interface['CONFSTATE'] = 'EXCL'
            if interface['is_vf'] and interface['CONFSTATE'] == 'DELETE':
                # revert this as '-' , as DELETE state means nothing for VFs
                interface['CONFSTATE'] = '-'

        return interfaces

    def _compute_routing_table_name(self, interface_info):
        """
        Compute the routing table name for a givne interface
        return the name as str
        """
        if self._metadata is None:
            raise ValueError('no metadata avaialable')
        if self._metadata['instance']['shape'].startswith('BM'):
            return 'ort%svl%s' % (interface_info['NIC_I'], interface_info['VLTAG'])
        return 'ort%s' % interface_info['IND']

    def _auto_deconfig_intf_routing(self, intf_infos):
        """
        Deconfigure interface routing
        parameter:
        intf_info: interface info as dict
            keys: see VNICUTils.get_network_config

        Raise:
            Exception. if configuration failed
        """
        # for namespaces the subnet and default routes will be auto deleted with the namespace
        if not intf_infos.has('NS'):
            _route_table_name = self._compute_routing_table_name(intf_infos)
            # TODO: rename method to remove_ip_rules
            NetworkHelpers.remove_ip_addr_rules(_route_table_name)
            NetworkHelpers.delete_route_table(_route_table_name)

    def _auto_config_intf_routing(self, net_namespace_info, intf_infos):
        """
        Configure interface routing
        parameter:
        net_namespace_info:
            information about namespace (or None if no namespace use)
            keys:
            name : namespace name
            start_sshd: if True start sshd within the namespace
        intf_info: interface info as dict
            keys: see VNICITils.get_network_config

        Raise:
            Exception. if configuration failed
        """

        _intf_to_use = intf_infos['IFACE']
        if self._metadata['instance']['shape'].startswith('BM') and intf_infos['VLTAG'] != "0":
            # in that case we operate on the VLAN tagged intf no
            _intf_to_use = '%sv%s' % (intf_infos['IFACE'], intf_infos['VLTAG'])

        if net_namespace_info:
            _logger.debug("default route add")
            ret, out = NetworkHelpers.add_static_ip_route(
                'default', 'via', intf_infos['VIRTRT'], namespace=net_namespace_info['name'])
            if ret != 0:
                raise Exception("cannot add namespace %s default gateway %s: %s" %
                                (net_namespace_info['name'], intf_infos['VIRTRT'], out))
            _logger.debug("added namespace %s default gateway %s", net_namespace_info['name'], intf_infos['VIRTRT'])
            if net_namespace_info['start_sshd']:
                ret = sudo_utils.call(['/usr/sbin/ip', 'netns', 'exec', net_namespace_info['name'], '/usr/sbin/sshd'])
                if ret != 0:
                    raise Exception("cannot start ssh daemon")
                _logger.debug('sshd daemon started')
        else:
            _route_table_name = self._compute_routing_table_name(intf_infos)

            NetworkHelpers.add_route_table(_route_table_name)

            _logger.debug("default route add")
            ret, out = NetworkHelpers.add_static_ip_route(
                'default', 'via', intf_infos['VIRTRT'], 'dev', _intf_to_use, 'table', _route_table_name)
            if ret != 0:
                raise Exception("cannot add default route via %s on %s to table %s" %
                                (intf_infos['VIRTRT'], _intf_to_use, _route_table_name))
            _logger.debug("added default route via %s dev %s table %s",
                          intf_infos['VIRTRT'], _intf_to_use, _route_table_name)

            # create source-based rule to use table
            ret, out = NetworkHelpers.add_static_ip_rule('from', intf_infos['ADDR'], 'lookup', _route_table_name)
            if ret != 0:
                raise Exception("cannot add rule from %s use table %s" % (intf_infos['ADDR'], _route_table_name))

            _logger.debug("added rule for routing from %s lookup %s with default via %s",
                          intf_infos['ADDR'], _route_table_name, intf_infos['VIRTRT'])

    def _config_secondary_intf(self, intf_infos):
        """
        Configures interface secodnary IPs

        parameter:
        intf_info: interface info as dict
            keys: see VNICITils.get_network_config

        Raise:
            Exception. if configuration failed
        """

        _route_table_name = self._compute_routing_table_name(intf_infos)

        _sec_addrs = []
        if intf_infos.has('SECONDARY_ADDRS'):
            _sec_addrs = intf_infos.get('SECONDARY_ADDRS')

        for secondary_ip in intf_infos['MISSING_SECONDARY_IPS']:
            _logger.debug("adding secondary IP address %s to interface (or VLAN) %s",
                          secondary_ip, intf_infos['IFACE'])

            NetworkInterfaceSetupHelper(intf_infos).add_secondary_address(secondary_ip)

            NetworkHelpers.add_route_table(_route_table_name)

            ret, _ = NetworkHelpers.add_static_ip_rule('from', secondary_ip, 'lookup', _route_table_name)
            if ret != 0:
                raise Exception("cannot add rule from %s use table %s" % (secondary_ip, _route_table_name))
            _logger.debug("added rule for routing from %s lookup %s with default via %s",
                          secondary_ip, _route_table_name, intf_infos['VIRTRT'])


def _auto_config_intf(net_namespace_info, intf_infos):
    """
    Configures interface

    parameter:
    net_namespace_info:
        information about namespace (or None if no namespace use)
        keys:
        name : namespace name
        start_sshd: if True start sshd within the namespace
    intf_info: interface info as dict
        keys: see VNICITils.get_network_config

    Raise:
        Exception. if configuration failed
    """
    # if interface is not up bring it up
    if intf_infos['STATE'] != 'up':
        _logger.debug('Bringing intf [%s] up ', intf_infos['IFACE'])
        ret = sudo_utils.call(['/usr/sbin/ip', 'link', 'set', 'dev', intf_infos['IFACE'], 'up'])
        if ret != 0:
            raise Exception('Cannot bring interface up')

    # create network namespace if needed
    if net_namespace_info is not None:
        if not NetworkHelpers.is_network_namespace_exists(net_namespace_info['name']):
            _logger.debug('creating namespace [%s]', net_namespace_info['name'])
            NetworkHelpers.create_network_namespace(net_namespace_info['name'])
        NetworkInterfaceSetupHelper(intf_infos, net_namespace_info['name']).setup()
    else:
        NetworkInterfaceSetupHelper(intf_infos).setup()


def _auto_deconfig_intf(intf_infos):
    """
    Deconfigures interface

    parameter:

    intf_info: interface info as dict
    keys: see VNICITils.get_network_config

    Raise:
        Exception. if configuration failed
    """
    if intf_infos.has('NS'):
        NetworkHelpers.kill_processes_in_namespace(intf_infos['NS'])
    # TODO EJANNET : LOOP on ('SECONDARY_ADDRS')
    #    -> NetworkInterfaceSetupHelper(intf_infos).remove_secondary_address()
    NetworkInterfaceSetupHelper(intf_infos).tear_down()

    # delete namespace
    if intf_infos.has('NS'):
        _logger.debug('deleting namespace [%s]', intf_infos['NS'])
        NetworkHelpers.destroy_network_namespace(intf_infos['NS'])

    NetworkHelpers.add_mac_to_nm(intf_infos['MAC'])
