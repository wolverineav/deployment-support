
# max number of threads, each thread sets up one node
MAX_WORKERS = 20

# root access to all the nodes is required
DEFAULT_USER = 'root'

# key words to specify node role in yaml config
ROLE_NEUTRON_SERVER = 'controller'
ROLE_COMPUTE        = 'compute'

# deployment t6/t5
T6 = 't6'
T5 = 't5'

# openstack release to bsnstacklib version
OS_RELEASE_TO_BSN_LIB = { 'juno' : '2015.1',
    'kilo'   : '2015.2',
    'liberty': '2016.1',
}

# horizon patch
DEPLOY_HORIZON_PATCH = True
HORIZON_PATCH_URL = {
    'juno' : 'https://github.com/bigswitch/horizon/archive/juno-bcf-3.0-beta1.tar.gz',
}
HORIZON_PATCH_DIR = {
    'juno' : 'horizon-juno-bcf-3.0-beta1',
}
HORIZON_BASE_DIR = '/usr/share/openstack-dashboard'

# constant file, directory names for each node
PRE_REQUEST_BASH     = 'pre_request.sh'
DST_DIR              = '/tmp'
GENERATED_SCRIPT_DIR = 'generated_script'
BASH_TEMPLATE_DIR    = 'bash_template'
PUPPET_TEMPLATE_DIR  = 'puppet_template'
SELINUX_TEMPLATE_DIR = 'selinux_template'
OSPURGE_TEMPLATE_DIR = 'ospurge_template'
LOG_FILE             = "/var/log/bcf_setup.log"

# constants for ivs config
INBAND_VLAN     = 4092
IVS_DAEMON_ARGS = (r'''DAEMON_ARGS=\"--syslog --inband-vlan %(inband_vlan)d%(uplink_interfaces)s%(internal_ports)s\"''')

# constants of supported OSes and versions
CENTOS          = 'centos'
CENTOS_VERSIONS = ['7']
UBUNTU          = 'ubuntu'
UBUNTU_VERSIONS = ['14']

# OSes that uses rpm or deb packages
RPM_OS_SET = [CENTOS]
DEB_OS_SET = [UBUNTU]

# regular expressions
EXISTING_NETWORK_VLAN_RANGE_EXPRESSION  = '^\s*network_vlan_ranges\s*=\s*(\S*)\s*:\s*(\S*)\s*:\s*(\S*)\s*$'
NETWORK_VLAN_RANGE_EXPRESSION   = '^\s*(\S*)\s*:\s*(\S*)\s*:\s*(\S*)\s*$'
VLAN_RANGE_CONFIG_PATH          = '/etc/neutron/plugins/ml2/ml2_conf.ini'
SELINUX_MODE_EXPRESSION         = '^\s*SELINUX\s*=\s*(\S*)\s*$'
SELINUX_CONFIG_PATH             = '/etc/selinux/config'


# openrc
FUEL_OPENRC            = '/root/openrc'
PACKSTACK_OPENRC       = '/root/keystonerc_admin'
MANUAL_OPENRC          = '/root/admin-openrc.sh'

# fuel constants
NONE_IP                = 'none'
BR_KEY_PRIVATE         = 'private'
BR_NAME_PRIVATE        = 'br-prv'
BR_NAME_INT            = 'br-int'
BR_KEY_MGMT            = 'management'
BR_KEY_EXCEPTION       = ['fw-admin', BR_KEY_PRIVATE]
# these ovs bridges needs to be cleaned up from compute node,
# but this script doesn't program any membership rules to bcf
# controller about these bridges.
TO_BE_CLEANED_BR_NAME  = [BR_NAME_INT, BR_NAME_PRIVATE]
OS_MGMT_TENANT         = 'os-mgmt'
HASH_HEADER            = 'BCF-SETUP'
BCF_CONTROLLER_PORT    = 8443
ANY                    = 'any'

# this map is not used in the script, but is
# helpful to understand the fuel created bridge
# and BCF membership rules
FUEL_GUI_TO_BR_KEY_MAP = {'management' : BR_KEY_MGMT,
    'storage' : 'storage',
    'public'  : 'ex',
    'private' : BR_KEY_PRIVATE}


