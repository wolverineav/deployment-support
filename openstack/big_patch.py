# Copyright 2014 Big Switch Networks, Inc.
# All Rights Reserved.
#
# @author: Kevin Benton
import argparse
import collections
import json
import netaddr
import os
import tempfile
import re
import subprocess
import time
import threading
import urllib2
try:
    import yaml
except:
    pass

# Arbitrary identifier printed in output to make tracking easy
BRANCH_ID = 'bcf-2.5'
SCRIPT_VERSION = '1.1.4'

# Maximum number of threads to deploy to nodes concurrently
MAX_THREADS = 20

# each entry is a 3-tuple naming the python package,
# the relative path inside the package, and the source URL
# e.g.
#  ('neutron', 'plugins/bigswitch/plugin.py',
#   'https://raw.githubusercontent.com/bigswitch/neutron/'
#   'stable/icehouse/neutron/plugins/bigswitch/plugin.py'),
PYTHON_FILES_TO_PATCH = []

# path to neutron tar.gz URL and local filename for offline use
HORIZON_TGZ_PATH = {
    'icehouse': ('https://github.com/bigswitch/horizon/archive/'
                 'stable/icehouse-bcf-2.0.0.tar.gz',
                 'horizon_stable_icehouse.tar.gz'),
    'juno': None
}
NEUTRON_TGZ_PATH = {
    'icehouse': ('https://github.com/bigswitch/neutron/archive/'
                 'stable/icehouse-bcf-2.0.0.tar.gz',
                 'neutron_stable_icehouse.tar.gz'),
    'juno': ('https://github.com/bigswitch/neutron/archive/'
             'stable/juno.tar.gz',
             'neutron_stable_juno.tar.gz')
}

# paths to extract from tgz to local horizon install. Don't include
# slashes on folders because * copying is not used.
HORIZON_PATHS_TO_COPY = (
    'LAST_NON_MERGE_COMMIT',
    'openstack_dashboard/dashboards/admin/dashboard.py',
    'openstack_dashboard/dashboards/project/dashboard.py',
    'openstack_dashboard/dashboards/admin/connections',
    'openstack_dashboard/dashboards/project/connections')


class TimedCommand(object):
    def __init__(self, cmd):
        self.cmd = cmd
        self.process = None
        self.retries = 0
        self.resp = None
        self.errors = None

    def run(self, timeout=60, retries=0):
        def target():
            self.process = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self.resp, self.errors = self.process.communicate()

        thread = threading.Thread(target=target)
        thread.start()

        thread.join(timeout)
        if thread.is_alive():
            self.process.terminate()
            thread.join()
            if self.retries < retries:
                self.retries += 1
                return self.run(timeout, retries)
            self.errors = (
                "Timed out waiting for command '%s' to finish." % self.cmd)

        return self.resp, self.errors


class Environment(object):

    nodes = []
    bigswitch_auth = None
    bigswitch_servers = None
    neutron_id = 'neutron'
    extra_template_params = {}
    offline_mode = False
    check_interface_errors = True
    bond_mode = 2

    def run_command_on_node(self, node, command, timeout=60, retries=0):
        raise NotImplementedError()

    def copy_file_to_node(self, node, local_path, remote_path):
        raise NotImplementedError()

    @property
    def network_vlan_ranges(self):
        raise NotImplementedError()

    def get_node_bond_interfaces(self, node):
        raise NotImplementedError()

    # where the physical interfaces should be added
    def get_node_phy_bridge(self, node):
        raise NotImplementedError()

    # associations of physnets to bridges
    def get_node_bridge_mappings(self, node):
        raise NotImplementedError()

    def set_neutron_id(self, neutron_id):
        if not neutron_id:
            raise Exception("A non-empty cluster-id must be specified.")
        self.neutron_id = neutron_id

    def set_bigswitch_servers(self, servers):
        for s in servers.split(','):
            try:
                int(s.split(':')[1])
            except:
                raise Exception('Invalid server "%s".\n'
                                'Format should be ip:port' % s)
        self.bigswitch_servers = servers

    def set_bigswitch_auth(self, auth):
        if ':' not in auth:
            raise Exception('Invalid credentials "%s".\n'
                            'Format should be user:pass' % auth)
        self.bigswitch_auth = auth

    def set_extra_template_params(self, dictofparams):
        self.extra_template_params = dictofparams

    def set_offline_mode(self, offline_mode):
        self.offline_mode = offline_mode

    def get_node_python_package_path(self, node, package):
        com = ('python -c "import %s;import os;print '
               'os.path.dirname(%s.__file__)"'
               % (package, package))
        resp, errors = self.run_command_on_node(node, com)
        if errors or not resp.strip() or len(resp.strip().splitlines()) > 1:
            if 'ImportError' in errors:
                return False
            raise Exception("Error retrieving path to python package '%s' on "
                            "node '%s'.\n%s\n%s" % (package, node,
                                                    errors, resp))
        return resp.strip()


class ConfigEnvironment(Environment):

    network_vlan_ranges = None

    def __init__(self, yaml_string, skip_nodes=[], specific_nodes=[]):
        try:
            self.settings = yaml.load(yaml_string)
        except Exception as e:
            raise Exception("Error loading from yaml file:\n%s" % e)
        if not isinstance(self.settings.get('nodes'), list):
            raise Exception("Missing nodes in yaml data.\n%s" % self.settings)
        if not self.settings.get('network_vlan_ranges'):
            raise Exception('Missing network_vlan_ranges in config.\n%s'
                            % self.settings)
        self.network_vlan_ranges = self.settings.get('network_vlan_ranges')
        try:
            self.nodes = [n['hostname'] for n in self.settings['nodes']
                          if n['hostname'] not in skip_nodes and
                          (not specific_nodes or
                           n['hostname'] in specific_nodes)]
        except KeyError:
            raise Exception('missing hostname in nodes %s'
                            % self.settings['nodes'])

    def get_node_bond_interfaces(self, node):
        for n in self.settings['nodes']:
            if n['hostname'] == node:
                return [i for i in n.get('bond_interfaces', '').split(',')
                        if i]
        print 'Node %s has no bond interfaces.' % node
        return []

    def get_node_bridge_mappings(self, node):
        br_mappings = None
        for n in self.settings['nodes']:
            if n['hostname'] == node:
                br_mappings = n.get('bridge_mappings')
        if not br_mappings:
            raise Exception('Node %s is missing bridge_mappings '
                            'which is required for the OVS agent.'
                            % node)
        cleaned = []
        for m in br_mappings.rstrip(',').split(','):
            if len(m.split(':')) != 2:
                raise Exception('Invalid bridge_mappings setting for node %s. '
                                'bridge_mappings should be a comma-separated '
                                'list of physnetName:bridgeName pairs.\n'
                                'Input -> %s ' % (br_mappings))
            cleaned.append(m.strip())
        return ','.join(cleaned)

    def get_node_phy_bridge(self, node):
        phy_br = None
        for n in self.settings['nodes']:
            if n['hostname'] == node:
                phy_br = n.get('physical_interface_bridge')
        if not phy_br:
            raise Exception('Node %s is missing physical_interface_bridge '
                            'which is required for bonding configuration.'
                            % node)
        return phy_br

    def copy_file_to_node(self, node, local_path, remote_path):
        resp, errors = TimedCommand(
            ["scp", '-o LogLevel=quiet', local_path, "root@%s:%s" % (node, remote_path)]
        ).run(timeout=180)
        return resp, errors

    def run_command_on_node(self, node, command, timeout=60, retries=0):
        resp, errors = TimedCommand(
            ["ssh", '-oStrictHostKeyChecking=no',
             '-o LogLevel=quiet', "root@%s" % node, command]
        ).run(timeout, retries)
        return resp, errors


class FuelEnvironment(Environment):

    def __init__(self, environment_id, skip_nodes=[], specific_nodes=[]):
        self.node_settings = {}
        self.nodes = []
        self.settings = {}
        try:
            print "Retrieving general Fuel settings..."
            output, errors = TimedCommand(
                ["fuel", "--json", "--env", str(environment_id),
                 "settings", "-d"]).run()
        except Exception as e:
            raise Exception("Error encountered trying to execute the Fuel "
                            "CLI:\n%s" % e)
        if errors:
            raise Exception("Error Loading cluster %s:\n%s"
                            % (environment_id, errors))
        try:
            path = output.split('downloaded to ')[1].rstrip()
        except (IndexError, AttributeError):
            raise Exception("Could not download fuel settings: %s"
                            % output)
        try:
            self.settings = json.loads(open(path, 'r').read())
        except Exception as e:
            raise Exception("Error parsing fuel json settings.\n%s" % e)

        # grab list of hosts
        print "Retrieving list of Fuel nodes..."
        output, errors = TimedCommand(
            ["fuel", "nodes", "--env", str(environment_id)]).run()
        if errors:
            raise Exception("Error Loading node list %s:\n%s"
                            % (environment_id, errors))
        try:
            lines = [l for l in output.splitlines()
                     if '----' not in l and 'pending_roles' not in l]
            nodes = [str(netaddr.IPAddress(l.split('|')[4].strip()))
                     for l in lines]
            self.nodes = [n for n in nodes if n not in skip_nodes and
                          (not specific_nodes or n in specific_nodes)]
            print "Nodes to configure: %s" % self.nodes
        except IndexError:
            raise Exception("Could not parse node list:\n%s" % output)
        for node in self.nodes:
            self.node_settings[node] = self.get_node_config(node)

    def copy_file_to_node(self, node, local_path, remote_path):
        resp, errors = TimedCommand(["scp", '-o LogLevel=quiet', local_path,
                                     "root@%s:%s" % (node, remote_path)]).run()
        return resp, errors

    def run_command_on_node(self, node, command, timeout=60, retries=0):
        resp, errors = TimedCommand(
            ["ssh", '-oStrictHostKeyChecking=no',
             '-o LogLevel=quiet', "root@%s" % node, command]
        ).run(timeout, retries)
        return resp, errors

    def get_node_config(self, node):
        print "Retrieving Fuel configuration for node %s..." % node
        resp, errors = TimedCommand(["ssh", '-oStrictHostKeyChecking=no',
                                     '-o LogLevel=quiet',
                                     "root@%s" % node,
                                     "cat", "/etc/astute.yaml"]).run()
        if errors or not resp:
            raise Exception("Error retrieving config for node %s:\n%s\n"
                            "Is the node online?"
                            % (node, errors))
        try:
            conf = yaml.load(resp)
        except Exception as e:
            raise Exception("Error parsing node yaml file:\n%s\n%s"
                            % (e, resp))
        return conf

    @property
    def network_vlan_ranges(self):
        net_vlans = []
        # comes from compute settings file
        node = self.node_settings.keys()[0]
        physnets = self.node_settings[node][
            'quantum_settings']['L2']['phys_nets']
        for physnet in physnets:
            range = physnets[physnet]['vlan_range']
            if not range:
                continue
            net_vlans.append('%s:%s' % (physnet, range))
        return ','.join(net_vlans)

    def get_node_bond_interfaces(self, node):
        if node not in self.nodes:
            raise Exception('No node in fuel environment %s' % node)
        trans = self.node_settings[node]['network_scheme']['transformations']
        for t in trans:
            if t.get('action') == 'add-bond':
                return t.get('interfaces', [])
        print 'Node %s has no bond interfaces.' % node
        return []

    def get_node_phy_bridge(self, node):
        bridge = None
        if node not in self.nodes:
            raise Exception('No node in fuel environment %s' % node)
        trans = self.node_settings[node]['network_scheme']['transformations']
        for t in trans:
            if t.get('action') == 'add-bond':
                bridge = t.get('bridge')
        if not bridge:
            raise Exception('Node %s has no bridge for the bond.' % node)
        return bridge

    def get_node_bridge_mappings(self, node):
        if node not in self.nodes:
            raise Exception('No node in fuel environment %s' % node)
        # NOTE: only one used network vlan range supported for now
        physnet = self.network_vlan_ranges.split(',')[0].split(':')[0]
        bridge = self.node_settings[node]['network_scheme']['roles']['private']
        return '%s:%s' % (physnet, bridge)


class StandaloneEnvironment(Environment):
    network_vlan_ranges = None
    nodes = ['localhost']

    def __init__(self, network_vlan_ranges, bridge_mappings, bond_interfaces,
                 physical_bridge):
        self.network_vlan_ranges = network_vlan_ranges
        # optional
        bond_interfaces = bond_interfaces or ''
        self.bridge_mappings = bridge_mappings
        self.bond_interfaces = [i for i in bond_interfaces.split(',') if i]
        self.phy_bridge = physical_bridge

    def get_node_bond_interfaces(self, node):
        return self.bond_interfaces

    def get_node_phy_bridge(self, node):
        return self.phy_bridge

    def get_node_bridge_mappings(self, node):
        if not self.bridge_mappings:
            raise Exception("Missing bridge_mappings setting")
        cleaned = []
        for m in self.bridge_mappings.rstrip(',').split(','):
            if len(m.split(':')) != 2:
                raise Exception('Invalid bridge_mappings setting for node %s. '
                                'bridge_mappings should be a comma-separated '
                                'list of physnetName:bridgeName pairs.\n'
                                'Input -> %s ' % (self.bridge_mappings))
            cleaned.append(m.strip())
        return ','.join(cleaned)

    def copy_file_to_node(self, node, local_path, remote_path):
        resp, errors = TimedCommand(
            ['bash', '-lc', "cp %s %s" % (local_path, remote_path)]).run()
        return resp, errors

    def run_command_on_node(self, node, command, timeout=60, retries=0):
        resp, errors = TimedCommand(['bash', '-lc', command]).run(timeout,
                                                                  retries)
        return resp, errors


class ConfigDeployer(object):
    def __init__(self, environment, openstack_release,
                 patch_python_files=True):
        self.env = environment
        self.os_release = openstack_release.lower()
        self.patch_python_files = patch_python_files
        self.patch_file_cache = {}
        if any([not self.env.bigswitch_auth,
                not self.env.bigswitch_servers,
                not self.env.nodes]):
            raise Exception('Environment must have at least 1 node '
                            'and controller options')
        if self.patch_python_files:
            if self.env.offline_mode:
                print 'Loading offline files...'
                for patch in (NEUTRON_TGZ_PATH[self.os_release],
                              HORIZON_TGZ_PATH[self.os_release]):
                    if not patch:
                        continue
                    try:
                        with open(os.path.join(os.path.dirname(__file__),
                                  patch[1]), 'r') as fh:
                            contents = fh.read()
                    except Exception as e:
                        raise Exception("Could not load offline archive of %s."
                                        "\nPlease download the archive and "
                                        "save it as %s.\nDetails: %s" %
                                        (patch[0], patch[1], str(e)))
                    self.patch_file_cache[patch[0]] = contents
            else:
                print 'Downloading patch files...'
                to_download = list(PYTHON_FILES_TO_PATCH)
                for lib in (NEUTRON_TGZ_PATH[self.os_release],
                            HORIZON_TGZ_PATH[self.os_release]):
                    if lib:
                        to_download.append(('', '', lib[0]))
                for patch in to_download:
                    url = patch[2]
                    try:
                        body = urllib2.urlopen(url).read()
                    except Exception as e:
                        raise Exception("Error encountered while trying to "
                                        "download patch file at %s.\n%s"
                                        % (url, e))
                    self.patch_file_cache[url] = body

    def deploy_to_all(self):
        thread_list = collections.deque()
        errors = []
        node_information = []
        for node in self.env.nodes:
            t = threading.Thread(target=self.deploy_to_node_catch_errors,
                                 args=(node, errors, node_information))
            thread_list.append(t)
            t.start()
            if len(thread_list) >= MAX_THREADS:
                top = thread_list.popleft()
                top.join()
        for thread in thread_list:
            thread.join()
        conn_strings = [info['neutron_connection']
                        for (node, info) in node_information]
        if len(set(conn_strings)) > 1:
            strings_with_nodes = ["%s: %s" % (node, info['neutron_connection'])
                                  for (node, info) in node_information]
            print ("Warning: different neutron connection strings detected on "
                   "neutron server nodes. They should all reference the same "
                   "database.\nConnections:\n%s" % "\n".join(strings_with_nodes))
        if errors:
            print "Encountered errors while deploying patch to nodes."
            for node, error in errors:
                print "Error on node %s:\n%s" % (node, error)
        else:
            print "Deployment Complete!"

    def deploy_to_node_catch_errors(self, node, error_log, node_information):
        try:
            self.deploy_to_node(node, node_information)
        except Exception as e:
            error_log.append((node, str(e)))

    def deploy_to_node(self, node, node_information):
        print "Applying configuration to %s..." % node
        bond_interfaces = self.env.get_node_bond_interfaces(node)
        puppet_settings = {
            'bond_interfaces': ','.join(bond_interfaces),
            'neutron_id': self.env.neutron_id,
            'bigswitch_servers': self.env.bigswitch_servers,
            'bigswitch_serverauth': self.env.bigswitch_auth,
            'network_vlan_ranges': self.env.network_vlan_ranges,
            'neutron_restart_refresh_only': str(
                not self.env.extra_template_params.get('force_services_restart',
                                                       False)
            ).lower(),
            'offline_mode': str(self.env.offline_mode).lower(),
            'bond_mode': self.env.bond_mode
        }
        if bond_interfaces:
            for bondint in bond_interfaces:
                resp, errors = self.env.run_command_on_node(
                    node, "ifconfig %s" % bondint)
                if not resp:
                    raise Exception("Error: bond member '%s' on node '%s' was "
                                    "not found.\n%s" % (bondint, node, errors))
                if 'inet addr' in resp:
                    raise Exception("Error: bond member '%s' on node '%s' has "
                                    "an IP address configured. Interfaces must"
                                    " not be in use.\nAddress: %s"
                                    % (bondint, node, resp))
                # warn on interface errors
                try:
                    tx = re.findall("TX packets:\d+ errors:(\d+) "
                                    "dropped:\d+ overruns:(\d+) "
                                    "carrier:(\d+)", resp)[0]
                    rx = re.findall("RX packets:\d+ errors:(\d+) "
                                    "dropped:\d+ overruns:(\d+) "
                                    "frame:(\d+)", resp)[0]
                    if (self.env.check_interface_errors
                            and any(map(int, rx + tx))):
                        print ("[Node %s] Warning: errors detected on bond "
                               "interface %s. Verify cabling and check error "
                               "rates using ifconfig.\n%s" %
                               (node, bondint, resp))
                except:
                    # ignore errors trying to parse
                    pass
            puppet_settings['physical_bridge'] = self.env.get_node_phy_bridge(
                node)
            physnets = self.env.network_vlan_ranges.split(',')
            if len(physnets) > 1:
                print ('Warning, multiple physnets configured "%s". A '
                       'bridge_mapping will only be configured for %s'
                       % (physnets, physnets[0]))
            puppet_settings['bridge_mappings'] = (
                self.env.get_node_bridge_mappings(node))
            for key, val in enumerate(bond_interfaces):
                puppet_settings['bond_int%s' % key] = val
            # If only one bond interface was provided, set the second bond
            # interface to the same as the first to prevent empty config errors
            if len(bond_interfaces) < 2:
                puppet_settings['bond_int1'] = bond_interfaces[0]
        ptemplate = PuppetTemplate(puppet_settings)
        ptemplate.settings['neutron_path'] = (
            self.env.get_node_python_package_path(node, 'neutron'))
        if self.patch_python_files:
            for package, rel_path, url in PYTHON_FILES_TO_PATCH:
                node_path = self.env.get_node_python_package_path(node,
                                                                  package)
                # package is not installed on this node
                # FIXME: Skipping 2.6 to workaround issues with CentOS
                if not node_path or '2.6' in node_path:
                    continue
                full_path = os.path.join(node_path, rel_path)
                contents = self.patch_file_cache[url]
                ptemplate.add_replacement_file(full_path, contents)
        pbody = ptemplate.get_string()
        f = tempfile.NamedTemporaryFile(delete=True)
        f.write(pbody)
        f.flush()
        remotefile = '~/generated_manifest.pp'
        resp, errors = self.env.copy_file_to_node(node, f.name, remotefile)
        if errors:
            raise Exception("error pushing puppet manifest to %s:\n%s"
                            % (node, errors))

        # Find where python libs are installed
        netaddr_path = self.env.get_node_python_package_path(node, 'netaddr')
        # we need to replace all of neutron plugins dir in CentOS
        if NEUTRON_TGZ_PATH[self.os_release] and netaddr_path:
            python_lib_dir = "/".join(netaddr_path.split("/")[:-1]) + "/"
            target_neutron_path = python_lib_dir + 'neutron'
            f = tempfile.NamedTemporaryFile(delete=True)
            f.write(self.patch_file_cache[NEUTRON_TGZ_PATH[self.os_release][0]])
            f.flush()
            nfile = '~/neutron.tar.gz'
            resp, errors = self.env.copy_file_to_node(node, f.name, nfile)
            if errors:
                raise Exception("error pushing neutron to %s:\n%s"
                                % (node, errors))
            # temp dir to extract to
            extract = "export TGT=$(mktemp -d);"
            # extract with strip-components to remove the branch dir
            extract += 'tar --strip-components=1 -xf '
            extract += '~/neutron.tar.gz -C "$TGT";'
            # move the extraced plugins to the neutron dir
            extract += 'yes | cp -rfp "$TGT/neutron" "%s/../";' % target_neutron_path
            # grab the commit marker
            extract += 'yes | cp -rfp "$TGT/LAST_NON_MERGE_COMMIT" "%s/../neutron/";' % target_neutron_path
            # cleanup old pyc files
            extract += 'find "%s" -name "*.pyc" -exec rm -rf {} \;' % target_neutron_path
            resp, errors = self.env.run_command_on_node(
                node, "bash -c '%s'" % extract)
            if errors:
                raise Exception("error installing neutron to %s:\n%s"
                                % (node, errors))

        # patch openstack_dashboard if available
        resp, errors = self.env.run_command_on_node(
            node,
            "updatedb && locate openstack_dashboard/dashboards/admin/dashboard.py | grep -v pyc")
        if (HORIZON_TGZ_PATH[self.os_release] and not errors and resp.splitlines()
                and 'openstack_dashboard/dashboards/admin/' in resp.splitlines()[0]):
            first = resp.splitlines()[0]
            f = tempfile.NamedTemporaryFile(delete=True)
            f.write(self.patch_file_cache[HORIZON_TGZ_PATH[self.os_release][0]])
            f.flush()
            nfile = '~/horizon.tar.gz'
            resp, errors = self.env.copy_file_to_node(node, f.name, nfile)
            if errors:
                raise Exception("error pushing horizon to %s:\n%s"
                                % (node, errors))
            # make sure horizon can read neutron conf
            for cf in ['/etc/neutron/neutron.conf', '/etc/neutron/plugin.ini',
                       '/etc/neutron/plugins/ml2/ml2_conf.ini',
                       '/etc/neutron/plugins/bigswitch/restproxy.ini']:
                self.env.run_command_on_node(node, 'chmod +r %s' % cf)
                self.env.run_command_on_node(
                    node, 'chmod +x %s' % cf.rsplit('/', 1)[0])
            base_dir = first.split('openstack_dashboard/dashboards/admin/')[0]
            # temp dir to extract to
            extract = "export TGT=$(mktemp -d);"
            # extract with strip-components to remove the branch dir
            extract += 'tar --strip-components=1 -xf '
            extract += '~/horizon.tar.gz -C "$TGT";'
            for horizon_patch in HORIZON_PATHS_TO_COPY:
                # remove filename
                if '/' in horizon_patch:
                    rel_target_dir = horizon_patch.rsplit('/', 1)[0] + '/'
                else:
                    # top level file
                    rel_target_dir = '/'
                extract += 'yes | cp -rfp "$TGT/%s" "%s/%s";' % (
                    horizon_patch, base_dir, rel_target_dir)
            # cleanup old pyc files
            extract += 'find "%s" -name "*.pyc" -exec rm -rf {} \;' % base_dir
            resp, errors = self.env.run_command_on_node(
                node, "bash -c '%s'" % extract)
            if errors:
                raise Exception("error installing horizon to %s:\n%s"
                                % (node, errors))

        if not self.env.offline_mode:
            self.env.run_command_on_node(
                node,
                "yum -y remove facter && gem install puppet facter "
                "--no-ri --no-rdoc")
            self.env.run_command_on_node(node, "ntpdate pool.ntp.org")
            self.env.run_command_on_node(node, "yum -y install python-pip")
            self.env.run_command_on_node(node, "yum -y install python-pbr")
            self.env.run_command_on_node(node, "apt-get install python-pip")
            self.env.run_command_on_node(node, "pip install pbr")
            resp, errors = self.env.run_command_on_node(
                node, "puppet module install puppetlabs-inifile --force", 30, 2)
            if errors:
                raise Exception("error installing puppet prereqs on %s:\n%s"
                                % (node, errors))
        log_name = ("log_for_generated_manifest-%s.log" %
                    time.strftime("%Y-%m-%d-%H-%M-%S", time.gmtime()))
        resp, errors = self.env.run_command_on_node(
            node, ("puppet apply %s --debug -l ~/%s" % (remotefile, log_name)),
            300, 2)
        if not errors:
            # with puppet, stderr goes to log
            errors, caterror = self.env.run_command_on_node(
                node, ("grep 'Puppet (err):' ~/%s" % log_name))
        # ignore bug in facter
        actual_errors = []
        errors = errors.splitlines()
        for e in errors:
            # ignore some warnings from facter that don't matter
            if "Device" in e and "does not exist." in e:
                continue
            if "Unable to add resolve nil for fact" in e:
                continue
            if "ls: cannot access /dev/s" in e:
                continue
            actual_errors.append(e)
        errors = '\n'.join(actual_errors)
        if errors:
            raise Exception("error applying puppet configuration to %s:\n%s"
                            % (node, errors))

        # run a few last sanity checks
        self.env.run_command_on_node(node, "service rabbitmq-server start")
        resp, errors = self.env.run_command_on_node(
            node, ("rabbitmqctl cluster_status | grep partitions | " +
                   r"""grep -v '\[\]'"""))
        if 'partitions' in resp:
            print ("Warning: RabbitMQ partition detected on node %s: %s "
                   "Restart rabbitmq-server on each node in the parition."
                   % (node, resp))

        # check for certificates generated in the future (due to clock change)
        # or expired certs
        certs = []
        resp, errors = self.env.run_command_on_node(
            node, ("cat /etc/keystone/keystone.conf | grep -e '^ca_certs' "
                   "| awk -F '=' '{ print $2 }'"))
        certs += resp.split(',') if resp else ['/etc/keystone/ssl/certs/ca.pem']
        resp, errors = self.env.run_command_on_node(
            node, ("cat /etc/keystone/keystone.conf | grep -e '^certfile' "
                   "| awk -F '=' '{ print $2 }'"))
        certs.append(
            resp if resp else '/etc/keystone/ssl/certs/signing_cert.pem')
        for cert in certs:
            cert = cert.strip()
            resp, errors = self.env.run_command_on_node(
                node, ("openssl verify %s" % cert))
            if 'expired' in resp or 'not yet valid' in resp:
                print ("Warning: the certificate %s being used by keystone is "
                       "not valid for the current time. If the clocks on the "
                       "servers are correct, the certificates will need to be "
                       "deleted and then regenerated using the "
                       "'keystone-manage pki_setup' command.\n"
                       "Details: %s" % (cert, resp))

        # check for lldpd
        resp, errors = self.env.run_command_on_node(
            node, ('ps -ef | grep lldpd | grep -v grep'))
        if not resp.strip():
            print ("Warning: lldpd process not running on node %s. "
                   "Automatic port groups will not be formed." % node)

        # check pbr prereq
        resp, errors = self.env.run_command_on_node(
            node, "python -c 'import pbr'")
        if errors.strip():
            print ("Warning: python pbr library missing on node %s. "
                   "Neutron processes may not start." % node)

        # check bond interface speeds match
        if bond_interfaces and self.env.check_interface_errors:
            speeds = {}
            for iface in bond_interfaces:
                resp, errors = self.env.run_command_on_node(
                    node, "ethtool %s | grep Speed" % iface)
                resp = resp.strip()
                if resp:
                    speeds[iface] = resp
            if len(set(speeds.values())) > 1:
                print ("Warning: bond interface speeds do not match on node "
                       "%s. Were the correct interfaces chosen?\nSpeeds: %s"
                       % (node, speeds))

        # collect connection string for comparison with other nodes
        neutron_running = self.env.run_command_on_node(
            node, 'ps -ef | grep neutron-server | grep -v grep')[0].strip()
        if neutron_running:
            resp = self.env.run_command_on_node(
                node, ("grep -R -e '^connection' /etc/neutron/neutron.conf")
            )[0].strip()
            if resp:
                node_information.append(
                    (node, {'neutron_connection': resp.replace(' ', '')})
                )
        print "Configuration applied to %s." % node


class PuppetTemplate(object):

    def __init__(self, settings):
        self.settings = {
            'bond_int0': '', 'bond_int1': '', 'bond_interfaces': '',
            'neutron_id': '', 'bigswitch_servers': '',
            'bigswitch_serverauth': '', 'network_vlan_ranges': '',
            'physical_bridge': 'br-ovs-bond0', 'bridge_mappings': '',
            'neutron_path': '', 'neutron_restart_refresh_only': '',
            'offline_mode': '', 'bond_mode': ''
        }
        self.files_to_replace = []
        for key in settings:
            self.settings[key] = settings[key]

    def get_string(self):
        # inject settings into template
        manifest = self.main_body % self.settings
        if self.settings['neutron_path']:
            manifest += self.neutron_body % self.settings
        # only setup bond stuff if interfaces are defined
        if self.settings['bond_interfaces']:
            manifest += self.bond_and_lldpd_configuration

        # inject all replacement files
        for path, contents in self.files_to_replace:
            escaped = contents.replace("\\", "\\\\").replace("'", "\\'")
            manifest += ("\nfile {'%(path)s':\nensure => file,"
                         "\npath => '%(path)s',\nmode => 0755,"
                         "\nnotify => Exec['restartneutronservices'],"
                         "\nrequire => Exec['neutronfilespresent'],"
                         "\ncontent => '%(escaped_content)s'\n}" %
                         {'path': path, 'escaped_content': escaped})
        return manifest

    def add_replacement_file(self, path, contents):
        """Adds a file that needs to be replaced.

        Path specifies the location on the server and contents will be the
        contents loaded into the file.
        """
        self.files_to_replace.append((path, contents))
        return self

    main_body = r"""
$neutron_id = '%(neutron_id)s'
$bigswitch_serverauth = '%(bigswitch_serverauth)s'
$bigswitch_servers = '%(bigswitch_servers)s'
$bond_interfaces = '%(bond_interfaces)s'
$network_vlan_ranges = '%(network_vlan_ranges)s'
$bond_int0 = '%(bond_int0)s'
$bond_int1 = '%(bond_int1)s'
$bond_name = 'ovs-bond0'
$phy_bridge = '%(physical_bridge)s'
$ovs_bridge_mappings = '%(bridge_mappings)s'
$neutron_restart_refresh_only = '%(neutron_restart_refresh_only)s'
# delay in milliseconds before bond interface is used after coming online
$bond_updelay = '15000'
# time in seconds between lldp transmissions
$lldp_transmit_interval = '5'
$offline_mode = %(offline_mode)s
$bond_mode = %(bond_mode)s
"""  # noqa
    neutron_body = r'''
if $operatingsystem == 'Ubuntu'{
    $neutron_conf_path = "/etc/neutron/plugins/ml2/ml2_conf.ini"
}
if $operatingsystem == 'CentOS' or $operatingsystem == 'RedHat'{
    $neutron_conf_path = "/etc/neutron/plugin.ini"
}
$neutron_base_conf_path = "/etc/neutron/neutron.conf"

$neutron_ovs_conf_path = "/etc/neutron/plugins/openvswitch/ovs_neutron_plugin.ini"
$neutron_l3_conf_path = '/etc/neutron/l3_agent.ini'
$neutron_dhcp_conf_path = '/etc/neutron/dhcp_agent.ini'
$neutron_main_conf_path = "/etc/neutron/neutron.conf"
$bigswitch_ssl_cert_directory = '/etc/neutron/plugins/ml2/ssl'


exec{'neutronfilespresent':
    onlyif => "python -c 'import neutron, os; print os.path.dirname(neutron.__file__)' && python -c 'import neutron, os; print os.path.dirname(neutron.__file__)' | xargs -I {} ls {}/plugins/bigswitch",
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    command => "echo",
}
# stop neutron server instances if there is no connection string configured
exec{"neutronserverrestart":
    refreshonly => true,
    command => 'bash -c \'grep -R "connection\s*=" /etc/neutron/* | grep -v "#" && service neutron-server restart || service neutron-server stop ||:\'',
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
}
if $operatingsystem == 'Ubuntu' {
  exec{"restartneutronservices":
      refreshonly => $neutron_restart_refresh_only,
      command => "/etc/init.d/neutron-plugin-openvswitch-agent restart ||:;",
      notify => [Exec['neutrondhcprestart'], Exec['neutronl3restart'], Exec['neutronserverrestart'], Exec['neutronmetarestart'], Exec['restartnovaservices'], Exec['ensurecoroclone']]
  }
}
if $operatingsystem == 'CentOS' {
  exec{"restartneutronservices":
      refreshonly => $neutron_restart_refresh_only,
      command => "/etc/init.d/openvswitch restart ||:; /etc/init.d/neutron-openvswitch-agent restart ||:;",
      notify => [Exec['checkagent'], Exec['neutrondhcprestart'], Exec['neutronl3restart'], Exec['neutronserverrestart'], Exec['neutronmetarestart'], Exec['restartnovaservices'], Exec['ensurecoroclone']]
  }
  exec{"checkagent":
      refreshonly => true,
      command => "[ $(ps -ef | grep openvswitch-agent | wc -l) -eq 0 ] && service neutron-openvswitch-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
  }
}
if $operatingsystem == 'RedHat' {
  exec{"restartneutronservices":
      refreshonly => $neutron_restart_refresh_only,
      command => "/usr/sbin/service neutron-openvswitch-agent restart ||:;",
      notify => [Exec['neutrondhcprestart'], Exec['neutronl3restart'], Exec['neutronserverrestart'], Exec['neutronmetarestart'], Exec['restartnovaservices'], Exec['ensurecoroclone']]
  }
  exec{"neutronl3restart":
      refreshonly => true,
      command => "/usr/sbin/service neutron-l3-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
  }
  exec{"neutronmetarestart":
      refreshonly => true,
      command => "/usr/sbin/service neutron-metadata-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
  }
  exec{"neutrondhcprestart":
      refreshonly => true,
      command => "/usr/sbin/service neutron-dhcp-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
  }
} else {
  exec{"neutronl3restart":
      refreshonly => true,
      command => "/etc/init.d/neutron-l3-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
      onlyif => "ls /etc/init.d/neutron-l3-agent"
  }
  exec{"neutronmetarestart":
      refreshonly => true,
      command => "/etc/init.d/neutron-metadata-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
      onlyif => "ls /etc/init.d/neutron-metadata-agent"
  }
  exec{"neutrondhcprestart":
      refreshonly => true,
      command => "/etc/init.d/neutron-dhcp-agent restart ||:;",
      path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
      onlyif => "ls /etc/init.d/neutron-dhcp-agent"
  }
}

$nova_services = 'rabbitmq-server nova-conductor nova-cert nova-consoleauth nova-scheduler nova-compute apache2 httpd'
exec{"restartnovaservices":
    refreshonly=> true,
    command => "bash -c 'pkill rabbitmq-server;sleep 1; pkill -U rabbitmq; sleep 2; for s in ${nova_services}; do (sudo service \$s restart &); (sudo service openstack-\$s restart &); echo \$s; done; sleep 5'",
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin"
}
exec{'ensurecoroclone':
    refreshonly=> true,
    command => 'bash -c \'crm configure clone clone_p_neutron-dhcp-agent p_neutron-dhcp-agent meta interleave="true" is-managed="true" target-role="Started"; crm configure clone clone_p_neutron-l3-agent p_neutron-l3-agent meta interleave="true" is-managed="true" target-role="Started"; echo 1\'',
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin"
}

# basic conf directories
$conf_dirs = ["/etc/neutron/plugins/ml2"]
file {$conf_dirs:
    ensure => "directory",
    owner => "neutron",
    group => "neutron",
    mode => 755,
    require => Exec['ensureovsagentconfig']
}

# ovs agent file may no be present, if so link to main conf so this script can
# modify the same thing
exec{'ensureovsagentconfig':
    command => "bash -c 'mkdir -p /etc/neutron/plugins/openvswitch/; ln -s /etc/neutron/neutron.conf $neutron_ovs_conf_path; echo 0'",
    path => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin"
}

exec{"heatconfexists":
    command => "bash -c 'mkdir /etc/heat/; touch /etc/heat/heat.conf; echo done'",
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin"
}
# use password for deferred authentication method for heat
# so users don't need extra roles.
ini_setting {"heat_deferred_auth_method":
  path => '/etc/heat/heat.conf',
  section  => 'DEFAULT',
  setting => 'deferred_auth_method',
  value => 'password',
  ensure => present,
  notify => Exec['restartheatservices'],
  require => Exec['heatconfexists']
}
$heat_services = 'heat-api heat-engine heat-api-cfn'
exec{"restartheatservices":
    refreshonly=> true,
    command => "bash -c 'for s in ${heat_services}; do (sudo service \$s restart &); (sudo service openstack-\$s restart &); echo \$s; done; sleep 5'",
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin"
}

# use two DHCP agents per network for redundancy
ini_setting {"dhcpnodespernetwork":
  path    => $neutron_main_conf_path,
  section => 'DEFAULT',
  setting => 'dhcp_agents_per_network',
  value   => '2',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}

# limit API workers for semaphore
ini_setting {"apiworkers":
  path    => $neutron_main_conf_path,
  section => 'DEFAULT',
  setting => 'api_workers',
  value   => '0',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
# limit RPC workers because they are experimental
ini_setting {"rpcworkers":
  path    => $neutron_main_conf_path,
  section => 'DEFAULT',
  setting => 'rpc_workers',
  value   => '0',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
# make sure ml2 is core plugin
ini_setting {"ml2core":
  path    => $neutron_main_conf_path,
  section => 'DEFAULT',
  setting => 'core_plugin',
  value   => 'ml2',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
# enable l3
ini_setting {"serviceplugins":
  path    => $neutron_main_conf_path,
  section => 'DEFAULT',
  setting => 'service_plugins',
  value   => 'router',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}

# reference ml2 ini from init script
file{'neutron_init_config':
  ensure => file,
  mode   => 0644,
  path   => '/etc/default/neutron-server',
  content =>"NEUTRON_PLUGIN_CONFIG='${neutron_conf_path}'\n",
  notify => Exec['restartneutronservices'],
}


# setup ml2 ini
ini_setting { "roothelper":
  path    => $neutron_conf_path,
  section => 'agent',
  setting => 'root_helper',
  value   => 'sudo neutron-rootwrap /etc/neutron/rootwrap.conf',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting { "secgroup":
  path    => $neutron_conf_path,
  section => 'securitygroup',
  setting => 'firewall_driver',
  value   => 'neutron.agent.linux.iptables_firewall.OVSHybridIptablesFirewallDriver',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting {"type_drivers":
  path    => $neutron_conf_path,
  section => 'ml2',
  setting => 'type_drivers',
  value   => 'vlan',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting {"tenant_network_types":
  path    => $neutron_conf_path,
  section => 'ml2',
  setting => 'tenant_network_types',
  value   => 'vlan',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting {"mechanism_drivers":
  path    => $neutron_conf_path,
  section => 'ml2',
  setting => 'mechanism_drivers',
  value   => 'openvswitch,bigswitch',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting {"vlan_ranges":
  path    => $neutron_conf_path,
  section => 'ml2_type_vlan',
  setting => 'network_vlan_ranges',
  value   => $network_vlan_ranges,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting {"ovs_bridge_mappings":
  path    => $neutron_ovs_conf_path,
  section => 'ovs',
  setting => 'bridge_mappings',
  value   => $ovs_bridge_mappings,
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"ovs_vlan_ranges":
  path    => $neutron_ovs_conf_path,
  section => 'ovs',
  setting => 'network_vlan_ranges',
  value   => $network_vlan_ranges,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"big_enable_tunneling":
  path    => $neutron_ovs_conf_path,
  section => 'OVS',
  setting => 'enable_tunneling',
  value   => 'False',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"enable_tunneling":
  path    => $neutron_ovs_conf_path,
  section => 'ovs',
  setting => 'enable_tunneling',
  value   => 'False',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"ovs_big_enable_tunneling":
  path    => $neutron_ovs_conf_path,
  section => 'OVS',
  setting => 'ovs_enable_tunneling',
  value   => 'False',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"ovs_enable_tunneling":
  path    => $neutron_ovs_conf_path,
  section => 'ovs',
  setting => 'ovs_enable_tunneling',
  value   => 'False',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"report_interval_main":
  path    => $neutron_main_conf_path,
  section => 'AGENT',
  setting => 'report_interval',
  value => 30,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"report_interval":
  path    => $neutron_conf_path,
  section => 'AGENT',
  setting => 'report_interval',
  value => 30,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"report_interval_main_lower":
  path    => $neutron_main_conf_path,
  section => 'agent',
  setting => 'report_interval',
  value => 30,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"report_interval_lower":
  path    => $neutron_conf_path,
  section => 'agent',
  setting => 'report_interval',
  value => 30,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"ovs_big_tunnel_types":
  path    => $neutron_ovs_conf_path,
  section => 'AGENT',
  setting => 'tunnel_types',
  ensure  => absent,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"ovs_tunnel_types":
  path    => $neutron_ovs_conf_path,
  section => 'agent',
  setting => 'tunnel_types',
  ensure  => absent,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"Ctbridge":
  path    => $neutron_ovs_conf_path,
  section => 'OVS',
  setting => 'tunnel_bridge',
  ensure  => absent,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"tbridge":
  path    => $neutron_ovs_conf_path,
  section => 'ovs',
  setting => 'tunnel_bridge',
  ensure  => absent,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"Bagentdown":
  path    => $neutron_base_conf_path,
  section => 'DEFAULT',
  setting => 'agent_down_time',
  value   => '75',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"agentdown":
  path    => $neutron_base_conf_path,
  section => 'DEFAULT',
  setting => 'agent_down_time',
  value   => '75',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"Bdbpoolover":
  path    => $neutron_base_conf_path,
  section => 'DATABASE',
  setting => 'max_overflow',
  value   => '30',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"dbpoolover":
  path    => $neutron_base_conf_path,
  section => 'database',
  setting => 'max_overflow',
  value   => '30',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"Brpcpoolc":
  path    => $neutron_conf_path,
  section => 'DEFAULT',
  setting => 'rpc_conn_pool_size',
  value   => '4',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"rpcpoolc":
  path    => $neutron_conf_path,
  section => 'DEFAULT',
  setting => 'rpc_conn_pool_size',
  value   => '4',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"Brpcpool":
  path    => $neutron_conf_path,
  section => 'DEFAULT',
  setting => 'rpc_thread_pool_size',
  value   => '4',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"rpcpool":
  path    => $neutron_conf_path,
  section => 'DEFAULT',
  setting => 'rpc_thread_pool_size',
  value   => '4',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"Bdbpool":
  path    => $neutron_base_conf_path,
  section => 'DATABASE',
  setting => 'max_pool_size',
  value   => '15',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"dbpool":
  path    => $neutron_base_conf_path,
  section => 'database',
  setting => 'max_pool_size',
  value   => '15',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"auto_failover":
  path    => $neutron_conf_path,
  section => 'DEFAULT',
  setting => 'allow_automatic_l3agent_failover',
  value   => 'True',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}
ini_setting {"neutron_id":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'neutron_id',
  value   => $neutron_id,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting {"bigswitch_servers":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'servers',
  value   => $bigswitch_servers,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting { "bigswitch_auth":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'server_auth',
  value   => $bigswitch_serverauth,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting { "bigswitch_auto_sync":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'auto_sync_on_failure',
  value   => 'True',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}
ini_setting { "bigswitch_consistency_interval":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'consistency_interval',
  value   => '60',
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}

ini_setting { "bigswitch_ssl":
  path    => $neutron_conf_path,
  section => 'restproxy',
  setting => 'ssl_cert_directory',
  value   => $bigswitch_ssl_cert_directory,
  ensure  => present,
  notify => Exec['restartneutronservices'],
  require => File[$conf_dirs],
}



file { 'ssl_dir':
  ensure => "directory",
  path   => $bigswitch_ssl_cert_directory,
  owner  => "neutron",
  group  => "neutron",
  purge => true,
  recurse => true,
  mode   => 0750,
  notify => Exec['neutronserverrestart'],
}

if $operatingsystem == 'Ubuntu'{
    file {'keystone':
      path   => '/usr/lib/python2.7/dist-packages/keystone-signing',
      ensure => 'directory',
      owner  => 'root',
      group  => 'root',
      mode   => 0777,
      notify => Exec['restartneutronservices'],
    }
}
if $operatingsystem == 'CentOS'{
    file {'keystone26':
      path   => '/usr/lib/python2.6/site-packages/keystone-signing',
      ensure => 'directory',
      owner  => 'root',
      group  => 'root',
      mode   => 0777,
      notify => Exec['restartneutronservices'],
    }
}
#configure dhcp agent
ini_setting {"dhcp_int_driver":
  path    => $neutron_dhcp_conf_path,
  section => 'DEFAULT',
  setting => 'interface_driver',
  value   => 'neutron.agent.linux.interface.OVSInterfaceDriver',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}

#configure l3 agent
ini_setting {"ext_net_bridge":
  path    => $neutron_l3_conf_path,
  section => 'DEFAULT',
  setting => 'external_network_bridge',
  value   => '',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}

ini_setting {"handle_internal_only":
  path    => $neutron_l3_conf_path,
  section => 'DEFAULT',
  setting => 'handle_internal_only_routers',
  value   => 'True',
  ensure  => present,
  notify => Exec['restartneutronservices'],
}

$MYSQL_USER='cat /etc/neutron/neutron.conf | grep "mysql://" | grep -v "#" | awk -F "//" \'{ print $2 }\' | awk -F ":" \'{ print $1 }\''
$MYSQL_PASS='cat /etc/neutron/neutron.conf | grep "mysql://" | grep -v "#" | awk -F "//" \'{ print $2 }\' | awk -F ":" \'{ print $2 }\' | awk -F "@" \'{ print $1 }\''
$MYSQL_HOST='cat /etc/neutron/neutron.conf | grep "mysql://" | grep -v "#" | awk -F "//" \'{ print $2 }\' | awk -F "@" \'{ print $2 }\' | awk -F "/" \'{ print $1 }\' | awk -F ":" \'{ print $1 }\''
$MYSQL_DB='cat /etc/neutron/neutron.conf | grep "mysql://" | grep -v "#" | awk -F "//" \'{ print $2 }\' | awk -F "@" \'{ print $2 }\' | awk -F "/" \'{ print $2 }\' | awk -F "?" \'{ print $1 }\''
$MYSQL_COM="mysql -u `$MYSQL_USER` -p`$MYSQL_PASS` -h `$MYSQL_HOST` `$MYSQL_DB`"
exec {"cleanup_neutron":
  onlyif => ["which mysql", "echo 'show tables' | $MYSQL_COM"],
  path => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
  command => "echo 'delete ports, floatingips from ports INNER JOIN floatingips on floatingips.floating_port_id = ports.id where ports.network_id NOT IN (select network_id from ml2_network_segments where network_type=\"vlan\");' | $MYSQL_COM;
              echo 'delete ports, routers from ports INNER JOIN routers on routers.gw_port_id = ports.id where ports.network_id NOT IN (select network_id from ml2_network_segments where network_type=\"vlan\");' | $MYSQL_COM;
              echo 'delete from ports where network_id NOT in (select network_id from ml2_network_segments where network_type=\"vlan\");' | $MYSQL_COM;
              echo 'delete from subnets where network_id NOT IN (select network_id from ml2_network_segments where network_type=\"vlan\");' | $MYSQL_COM;
              echo 'delete from networks where id NOT IN (select network_id from ml2_network_segments where network_type=\"vlan\");' | $MYSQL_COM;
              echo 'delete from ports where network_id NOT IN (select network_id from networks);' | $MYSQL_COM;
              echo 'delete from routers where gw_port_id NOT IN (select id from ports);' | $MYSQL_COM;
              echo 'delete from floatingips where floating_port_id NOT IN (select id from ports);' | $MYSQL_COM;
              echo 'delete from floatingips where fixed_port_id NOT IN (select id from ports);' | $MYSQL_COM;
              echo 'delete from subnets where network_id NOT IN (select id from networks);' | $MYSQL_COM;
             "
}

'''  # noqa

    bond_and_lldpd_configuration = r'''
exec {"loadbond":
   command => 'modprobe bonding',
   path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
   unless => "lsmod | grep bonding",
   notify => Exec['deleteovsbond'],
}
exec {"deleteovsbond":
  command => "/usr/bin/ovs-appctl bond/list | grep -v slaves | head -n1 | awk -F '\\t' '{ print \$1 }' | xargs -I {} ovs-vsctl del-port ${phy_bridge} {}",
  path    => "/usr/local/bin/:/bin/:/usr/bin",
  require => Exec['lldpdinstall'],
  onlyif  => "/sbin/ifconfig ${phy_bridge} && ovs-vsctl show | grep '\"${bond_name}\"'",
  notify => Exec['networkingrestart']
}
exec {"clearint0":
  command => "ovs-vsctl --if-exists del-port $bond_int0",
  path    => "/usr/local/bin/:/bin/:/usr/bin",
  require => Exec['lldpdinstall'],
  onlyif => "ovs-vsctl show | grep 'Port \"${bond_int0}\"'",
}
exec {"clearint1":
  command => "ovs-vsctl --if-exists del-port $bond_int1",
  path    => "/usr/local/bin/:/bin/:/usr/bin",
  require => Exec['lldpdinstall'],
  onlyif => "ovs-vsctl show | grep 'Port \"${bond_int1}\"'",
}

# make sure bond module is loaded
if $operatingsystem == 'Ubuntu' {
    file_line { 'bond':
       path => '/etc/modules',
       line => 'bonding',
       notify => Exec['loadbond'],
    }
    file {'bondmembers':
        ensure => file,
        path => '/etc/network/interfaces.d/bond',
        mode => 0644,
        content => "
auto ${bond_int0}
iface ${bond_int0} inet manual
bond-master bond0

auto ${bond_int1}
iface ${bond_int1} inet manual
bond-master bond0

auto bond0
    iface bond0 inet manual
    bond-mode ${bond_mode}
    bond-xmit_hash_policy 1
    bond-miimon 50
    bond-updelay ${bond_updelay}
    bond-slaves none
    ",
    }
    exec {"networkingrestart":
       refreshonly => true,
       require => [Exec['loadbond'], File['bondmembers'], Exec['deleteovsbond'], Exec['lldpdinstall']],
       command => '/etc/init.d/networking restart',
       notify => Exec['addbondtobridge'],
    }
    if ! $offline_mode {
        exec{"lldpdinstall":
            command => 'bash -c \'
              # default to 12.04
              export urelease=12.04;
              [ "$(lsb_release -r | tr -d -c 0-9)" = "1410" ] && export urelease=14.10;
              [ "$(lsb_release -r | tr -d -c 0-9)" = "1404" ] && export urelease=14.04;
              wget "http://download.opensuse.org/repositories/home:vbernat/xUbuntu_$urelease/Release.key";
              sudo apt-key add - < Release.key;
              echo "deb http://download.opensuse.org/repositories/home:/vbernat/xUbuntu_$urelease/ /"\
                  > /etc/apt/sources.list.d/universe.list;
              rm /var/lib/dpkg/lock ||:; rm /var/lib/apt/lists/lock ||:; apt-get update;
              apt-get -o Dpkg::Options::=--force-confdef install --allow-unauthenticated -y lldpd\'',
            path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
            notify => [Exec['networkingrestart'], File['ubuntulldpdconfig']],
        }
    } else {
        exec{"lldpdinstall":
            onlyif => "bash -c '! ls /etc/init.d/lldpd'",
            command => "echo noop",
            path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
            notify => [Exec['networkingrestart'], File['ubuntulldpdconfig']],
        }
    }
    file{"lldlcliwrapper":
        ensure => file,
        mode   => 0755,
        path   => '/usr/bin/lldpclinamewrap',
        content =>'#!/bin/bash
# this script forces lldpd to use the same hostname that openstack uses
(sleep 2 && echo "configure system hostname `uname -n`" | lldpcli &)
lldpcli $@
',
        notify => Exec['lldpdrestart'],
    }
    exec{"triggerinstall":
        onlyif => 'bash -c "! ls /etc/init.d/lldpd"',
        command => 'echo',
        notify => Exec['lldpdinstall'],
        path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }
    file{'ubuntulldpdconfig':
        ensure => file,
        mode   => 0644,
        path   => '/etc/default/lldpd',
        content => "DAEMON_ARGS='-S 5c:16:c7:00:00:00 -I ${bond_interfaces} -L /usr/bin/lldpclinamewrap'\n",
        notify => Exec['lldpdrestart'],
    }
    exec {"openvswitchrestart":
       refreshonly => true,
       command => '/etc/init.d/openvswitch-switch restart',
       path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }
}
if $operatingsystem == 'RedHat' {
    file{'selinux_allow_certs':
       ensure => file,
       mode => 0644,
       path => '/root/neutroncerts.te',
       content => '
module neutroncerts 1.0;

require {
        type neutron_t;
        type etc_t;
        class dir create;
        class file create;
}

#============= neutron_t ==============
allow neutron_t etc_t:dir create;
allow neutron_t etc_t:file create;
',
       notify => Exec["selinuxcompile"],
    }
    exec {"selinuxcompile":
       refreshonly => true,
       command => "bash -c 'semanage permissive -a neutron_t;
                   checkmodule -M -m -o /root/neutroncerts.mod /root/neutroncerts.te;
                   semodule_package -m /root/neutroncerts.mod -o /root/neutroncerts.pp;
                   semodule -i /root/neutroncerts.pp' ||:",
        path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }
    ini_setting{"neutron_service":
        path => "/usr/lib/systemd/system/neutron-server.service",
        section => "Service",
        setting => "Type",
        value => "simple",
        ensure => present,
        notify => Exec['reloadservicedef']
    }
    exec{"reloadservicedef":
        refreshonly => true,
        command => "systemctl daemon-reload",
        path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
        notify => Exec['restartneutronservices']
    }
    exec{"rabbitrestart":
        refreshonly => true,
        command => "sleep 5; service rabbitmq-server restart",
        path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
        subscribe => Exec['restartnovaservices'],
    }
    exec {"networkingrestart":
       refreshonly => true,
       command => '/etc/init.d/network restart',
       require => [Exec['loadbond'], File['bondmembers'], Exec['deleteovsbond']],
       notify => Exec['addbondtobridge'],
    }
    exec{"lldpdinstall":
        onlyif => "bash -c '! ls /etc/init.d/lldpd'",
        command => "ls",
        path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }
    file{'bondmembers':
        require => [Exec['loadbond']],
        ensure => file,
        mode => 0644,
        path => '/etc/sysconfig/network-scripts/ifcfg-bond0',
        content => "
DEVICE=bond0
USERCTL=no
BOOTPROTO=none
ONBOOT=yes
BONDING_OPTS='mode=${bond_mode} miimon=50 updelay=${bond_updelay} xmit_hash_policy=1'
",
    }
    file{'bond_int0config':
        require => File['bondmembers'],
        notify => Exec['networkingrestart'],
        ensure => file,
        mode => 0644,
        path => "/etc/sysconfig/network-scripts/ifcfg-$bond_int0",
        content => "DEVICE=$bond_int0\nMASTER=bond0\nSLAVE=yes\nONBOOT=yes\nUSERCTL=no\n",
    }
    if $bond_int0 != $bond_int1 {
        file{'bond_int1config':
            require => File['bondmembers'],
            notify => Exec['networkingrestart'],
            ensure => file,
            mode => 0644,
            path => "/etc/sysconfig/network-scripts/ifcfg-$bond_int1",
            content => "DEVICE=$bond_int1\nMASTER=bond0\nSLAVE=yes\nONBOOT=yes\nUSERCTL=no\n",
        }
    }

    exec {"openvswitchrestart":
       refreshonly => true,
       command => 'service openvswitch restart',
       path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }

}
if $operatingsystem == 'CentOS' {
    if ! $offline_mode {
        exec {'lldpdinstall':
           onlyif => "yum --version && (! ls /etc/init.d/lldpd)",
           command => "bash -c 'cd /etc/yum.repos.d/; wget http://download.opensuse.org/repositories/home:vbernat/CentOS_CentOS-6/home:vbernat.repo; yum -y install lldpd'",
           path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
           notify => File['centoslldpdconfig'],
        }
    } else {
        exec {'lldpdinstall':
           onlyif => "bash -c '! ls /etc/init.d/lldpd'",
           command => "echo noop",
           path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
           notify => File['centoslldpdconfig'],
        }
    }
    file{'centoslldpdconfig':
        ensure => file,
        mode   => 0644,
        path   => '/etc/sysconfig/lldpd',
        content => "LLDPD_OPTIONS='-S 5c:16:c7:00:00:00 -I ${bond_interfaces}'\n",
        notify => Exec['lldpdrestart'],
    }
    exec {"networkingrestart":
       refreshonly => true,
       command => '/etc/init.d/network restart',
       require => [Exec['loadbond'], File['bondmembers'], Exec['deleteovsbond'], Exec['lldpdinstall']],
       notify => Exec['addbondtobridge'],
    }
    file{'bondmembers':
        require => [Exec['lldpdinstall'],Exec['loadbond'],File['centoslldpdconfig']],
        ensure => file,
        mode => 0644,
        path => '/etc/sysconfig/network-scripts/ifcfg-bond0',
        content => "
DEVICE=bond0
USERCTL=no
BOOTPROTO=none
ONBOOT=yes
BONDING_OPTS='mode=${bond_mode} miimon=50 updelay=${bond_updelay} xmit_hash_policy=1'
",
    }
    file{'bond_int0config':
        require => File['bondmembers'],
        notify => Exec['networkingrestart'],
        ensure => file,
        mode => 0644,
        path => "/etc/sysconfig/network-scripts/ifcfg-$bond_int0",
        content => "DEVICE=$bond_int0\nMASTER=bond0\nSLAVE=yes\nONBOOT=yes\nUSERCTL=no\n",
    }
    if $bond_int0 != $bond_int1 {
        file{'bond_int1config':
            require => File['bondmembers'],
            notify => Exec['networkingrestart'],
            ensure => file,
            mode => 0644,
            path => "/etc/sysconfig/network-scripts/ifcfg-$bond_int1",
            content => "DEVICE=$bond_int1\nMASTER=bond0\nSLAVE=yes\nONBOOT=yes\nUSERCTL=no\n",
        }
    }
    exec {"openvswitchrestart":
       refreshonly => true,
       command => '/etc/init.d/openvswitch restart',
       path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
    }
}
exec {"ensurebridge":
  command => "ovs-vsctl --may-exist add-br ${phy_bridge}",
  path    => "/usr/local/bin/:/bin/:/usr/bin",
}
exec {"addbondtobridge":
   command => "ovs-vsctl --may-exist add-port ${phy_bridge} bond0",
   onlyif => "/sbin/ifconfig bond0 && ! ovs-ofctl show ${phy_bridge} | grep '(bond0)'",
   path    => "/usr/local/bin/:/bin/:/usr/bin",
   notify => Exec['openvswitchrestart'],
   require => Exec['ensurebridge'],
}
exec{'lldpdrestart':
    refreshonly => true,
    require => Exec['lldpdinstall'],
    command => "rm /var/run/lldpd.socket ||:;/etc/init.d/lldpd restart",
    path    => "/usr/local/bin/:/bin/:/usr/bin:/usr/sbin:/usr/local/sbin:/sbin",
}
file{'lldpclioptions':
    ensure => file,
    mode   => 0644,
    path   => '/etc/lldpd.conf',
    content => "configure lldp tx-interval ${lldp_transmit_interval}",
    notify => Exec['lldpdrestart'],
}
'''  # noqa

if __name__ == '__main__':
    print "Big Patch Version %s:%s" % (BRANCH_ID, SCRIPT_VERSION)
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    # 2.5 only supports icehouse
    #parser.add_argument("-r", "--openstack-release", required=True,
    #                    help="Name of OpenStack release (Juno or Icehouse).")
    group.add_argument("-i", "--stand-alone", action='store_true',
                       help="Configure the server running this script "
                            "(root privileges required).")
    group.add_argument("-f", "--fuel-environment",
                       help="Fuel environment ID to load node settings from.")
    group.add_argument("-c", "--config-file", type=argparse.FileType('r'),
                       help="Path to YAML config file for "
                            "non-Fuel deployments.")
    parser.add_argument("--neutron-cluster-name", default='neutron',
                        help="Name used to set origin field of the "
                             "objects created on the backend controllers.")
    parser.add_argument("-s", "--controllers", required=True,
                        help="Comma-separated list of "
                             "<controller:port> pairs.")
    parser.add_argument("-a", "--controller-auth", required=True,
                        help="Username and password <user:pass> "
                             "to connect to controller.")
    parser.add_argument('--skip-file-patching', action='store_true',
                        help="Do not patch openstack packages with updated "
                             "versions.")
    parser.add_argument('--force-services-restart', action='store_true',
                        help="Restart the Neutron and Nova services even if "
                             "no files or configuration options are changed.")
    parser.add_argument('--offline-mode', action='store_true',
                        help="Disable fetching files from the Internet. This "
                             "includes the neutron repo as well as the "
                             "prerequisites on the individual nodes.")
    parser.add_argument('--ignore-interface-errors', action='store_true',
                        help="Suppress warnings about interface errors.")
    parser.add_argument("--bond-mode", default="xor",
                        help="Mode to set on node bonds (xor or round-robin). "
                             "(Default is xor.)")
    remote = parser.add_argument_group('remote-deployment')
    remote.add_argument('--skip-nodes',
                        help="Comma-separate list of nodes to skip deploying "
                             "configurations to.")
    remote.add_argument('--specific-nodes',
                        help="Comma-separate list of nodes to deploy to. All "
                             "others will be skipped.")
    local = parser.add_argument_group(
        'standalone-deployment', 'Arguments for standalone deployments')
    local.add_argument('--network-vlan-ranges',
                       help="Comma-separated list of physical network vlan "
                            "ranges. (e.g. a single vlan range would be "
                            "physnet1:100:3000)")
    local.add_argument('--phy-int-bridge',
                       help="OVS bridge that will contain the bond "
                            "interface. (e.g. br-eth0)")
    local.add_argument('--bridge-mappings',
                       help="Mappings between physical networks and OVS "
                            "bridges. (e.g. physnet1:br-eth0)")
    local.add_argument('--bond-interfaces',
                       help="Comma-separated list of interfaces to configure. "
                            "(e.g. eth1,eth2)")
    args = parser.parse_args()
    if args.specific_nodes:
        specific_nodes = args.specific_nodes.split(',')
    else:
        specific_nodes = []
    if args.skip_nodes:
        skip_nodes = args.skip_nodes.split(',')
    else:
        skip_nodes = []
    neutron_id = args.neutron_cluster_name
    if args.fuel_environment:
        neutron_id = '%s-%s' % (neutron_id, args.fuel_environment)
        environment = FuelEnvironment(args.fuel_environment,
                                      skip_nodes=skip_nodes,
                                      specific_nodes=specific_nodes)
    elif args.config_file:
        environment = ConfigEnvironment(args.config_file.read(),
                                        skip_nodes=skip_nodes,
                                        specific_nodes=specific_nodes)
    elif args.stand_alone:
        if not args.network_vlan_ranges:
            parser.error('--network-vlan-ranges is required when using '
                         'stand-alone mode.')
        if args.bond_interfaces:
            missing = []
            if not args.phy_int_bridge:
                missing.append('--phy-int-bridge')
            if not args.bridge_mappings:
                missing.append('--bridge-mappings')
            if missing:
                parser.error('Missing required params %s when specifying bond '
                             'interfaces.' % missing)
        environment = StandaloneEnvironment(
            args.network_vlan_ranges, args.bridge_mappings,
            args.bond_interfaces, args.phy_int_bridge)
    else:
        parser.error('You must specify the Fuel environment, the config '
                     'file, or standalone mode.')
    allowed_bond_modes = {'xor': 2, 'round-robin': 0}
    if args.bond_mode not in allowed_bond_modes:
        parser.error('Unsupported bond mode: "%s". Supported modes: "%s"'
                     % (args.bond_mode, allowed_bond_modes))
    environment.bond_mode = allowed_bond_modes[args.bond_mode]
    environment.set_bigswitch_servers(args.controllers)
    environment.set_bigswitch_auth(args.controller_auth)
    environment.set_neutron_id(neutron_id)
    environment.set_offline_mode(args.offline_mode)
    environment.set_extra_template_params(
        {'force_services_restart': args.force_services_restart})
    if args.ignore_interface_errors:
        environment.check_interface_errors = False
    deployer = ConfigDeployer(environment,
                              patch_python_files=not args.skip_file_patching,
                              openstack_release='icehouse')
    deployer.deploy_to_all()
