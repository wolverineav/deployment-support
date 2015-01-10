# Copyright 2014 Big Switch Networks, Inc.
# All Rights Reserved.
#
# This script is used to set up cloud stack management node
# and compute nodes with Big Cloud Fabric. The requirements are:
# BCF 2.5
# installation node: ubuntu 12.04, centos 6.5 or centos 6.6
# management node: ubuntu 12.04, centos 6.5 or centos 6.6
# compute node: ubuntu 12.04, centos 6.5, centos 6.6 or xenserver 6.2
# 
# To prepare installation, on installation node, please download deb packages if it is ubuntu
# cloudstack-common_4.5.0-snapshot_all.deb,
# cloudstack-management_4.5.0-snapshot_all.deb,
# cloudstack-agent_4.5.0-snapshot_all.deb
# or rpm packages if it is centos
# cloudstack-common-4.5.0-SNAPSHOT.el6.x86_64.rpm
# cloudstack-awsapi-4.5.0-SNAPSHOT.el6.x86_64.rpm
# cloudstack-management-4.5.0-SNAPSHOT.el6.x86_64.rpm
# cloudstack-agent-4.5.0-SNAPSHOT.el6.x86_64.rpm
# and put them under the same directory as this script.
#
# On installation node, run
# sudo apt-get update
# sudo apt-get install -y sshpass python-yaml python-pip python-dev
# sudo pip install futures subprocess32
#
# On all compute nodes, make sure ssh is installed
# sudo apt-get install -y ssh (on all nodes)
#
# On installation node, edit example.yaml to reflect the physical setup, then
# sudo python ./big_patch.py -c example.yaml


'''
# Following is an example data structure 
# after parsing yaml configuration

example_config = dict(
    nodes = [
        dict(
            hostname = '172.16.54.130',
            role = ROLE_MGMT,
        ),
        dict(
            hostname = '172.16.54.132',
        ),
        dict(
            hostname = '172.16.54.134',
            node_username: username,
            node_password: password,
            pxe_interface: 'eth9',
            bond_interface = dict(
                interfaces = ['eth3','eth4'],
                name = 'bond1',
            ),
        ),
    ],

    mysql_root_pwd = 'bsn',
    cloud_db_pwd = 'bsn',
    management_vlan = 6,
    storage_vlan = 7,
    public_vlan = 5,
    guest_vlan = None,
    default_pxe_interface: 'eth0',
    default_node_username: 'bsn',
    default_node_password: 'bsn',
    default_role = ROLE_COMPUTE,
    default_bond_interface = dict(
        interfaces = ['eth1','eth2'],
        name = 'bond0',
    ),
)
'''

import os
import sys
import yaml
import time
import string
import Queue
import logging
import argparse
import threading
import collections
import subprocess32 as subprocess
from sets import Set
from threading import Lock

LOG_FILENAME = '/var/log/cloudstack_deploy.log'
logging.basicConfig(filename=LOG_FILENAME,level=logging.DEBUG)


RELEASE_NAME = 'IronHorse+'

# A cloudstack node can be either management or compute,
# There is by default only one management node.
ROLE_MGMT    = 'management'
ROLE_COMPUTE = 'compute'

# Maximum number of workers to deploy to nodes concurrently
MAX_WORKERS = 10

# undef string for puppet
UNDEF = ''

# cloud stack packages
CS_VERSION = '4.5.0'
CS_URL    = ('http://jenkins.bigswitch.com/job/cloudstack_ihplus_4.5/lastSuccessfulBuild/artifact')
CS_COMMON = ('cloudstack-common_%(cs_version)s-snapshot_all.deb' % {'cs_version' : CS_VERSION})
CS_MGMT   = ('cloudstack-management_%(cs_version)s-snapshot_all.deb' % {'cs_version' : CS_VERSION})
CS_AGENT  = ('cloudstack-agent_%(cs_version)s-snapshot_all.deb' % {'cs_version' : CS_VERSION})

CS_COMMON_RPM = ('cloudstack-common-%(cs_version)s-SNAPSHOT.el6.x86_64.rpm' % {'cs_version' : CS_VERSION})
CS_MGMT_RPM   = ('cloudstack-management-%(cs_version)s-SNAPSHOT.el6.x86_64.rpm' % {'cs_version' : CS_VERSION})
CS_AWSAPI_RPM = ('cloudstack-awsapi-%(cs_version)s-SNAPSHOT.el6.x86_64.rpm' % {'cs_version' : CS_VERSION})
CS_AGENT_RPM = ('cloudstack-agent-%(cs_version)s-SNAPSHOT.el6.x86_64.rpm' % {'cs_version' : CS_VERSION})

STORAGE_SCRIPT = '/usr/share/cloudstack-common/scripts/storage/secondary/cloud-install-sys-tmplt'
STORAGE_VM_URL = ('http://jenkins.buildacloud.org/view/master/job/'
                  'build-systemvm-master/lastStableBuild/artifact/tools/appliance/dist')
STORAGE_VM_TEMPLATE = 'systemvmtemplate-master-kvm.qcow2.bz2'

# compute os, can be ubuntu, centos or xenserver
COMPUTE_OS = 'ubuntu'

# management os, can be ubuntu or centos
MGMT_OS = 'ubuntu'

# master nodes for XEN server pools
MASTER_NODES = {}

# pool size
POOL_SIZES = {}

# management node
MANAGEMENT_NODE = None

# management node puppet template
MGMT_PUPPET = r'''
$user           = "%(user)s"
$mysql_root_pwd = "%(mysql_root_pwd)s"
$cloud_db_pwd   = "%(cloud_db_pwd)s"
$distro         = 'precise'
$cs_url         = "%(cs_url)s"
$cs_common      = "%(cs_common)s"
$cs_mgmt        = "%(cs_mgmt)s"
$storage_script = "%(storage_script)s"
$storage_vm_url = "%(storage_vm_url)s"
$storage_vm_template = "%(storage_vm_template)s"

class { 'apt':
    always_apt_update => true,
}

file_line {'mvn3_deb':
    path    => '/etc/apt/sources.list',
    line    => "deb http://ppa.launchpad.net/natecarlson/maven3/ubuntu ${distro} main",
    match   => "^deb http://ppa.launchpad.net/natecarlson/maven3/ubuntu.*$",
    require => File['/etc/apt/sources.list'],
}

file_line {'mvn3_src_deb':
    path    => '/etc/apt/sources.list',
    line    => "deb-src http://ppa.launchpad.net/natecarlson/maven3/ubuntu ${distro} main",
    match   => "^deb-src http://ppa.launchpad.net/natecarlson/maven3/ubuntu.*$",
    require => File['/etc/apt/sources.list'],
}

exec {"update":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "apt-get update",
    require => [File_Line['mvn3_deb'],
                File_line['mvn3_src_deb']],
    returns => [0, 100],
}


package {[
    'ethtool',
    'dbus',
    'qemu-kvm',
    'ubuntu-vm-builder',
    'nfs-kernel-server',
    'mysql-server',
    'mysql-client',
    'openjdk-7-jdk',
    'python-software-properties',
    'debhelper',
    'tomcat6',
    'genisoimage',
    'python-mysqldb',
    'augeas-lenses',
    'libaugeas0',
    'libcommons-daemon-java',
    'jsvc',
    'libmysql-java',
    'python-paramiko',
    'augeas-tools',
    ]:
    ensure  => installed,
    require => Exec['update'],
}

exec {'update jdk':
    subscribe   => Package["openjdk-7-jdk"],
    refreshonly => true,
    path        => "/bin:/usr/bin:/usr/sbin",
    command     => "update-java-alternatives -s java-1.7.0-openjdk-amd64",
}

exec {"set mysql password":
    subscribe   => Package["mysql-server"],
    refreshonly => true,
    path        => "/bin:/usr/bin",
    command     => "mysqladmin -uroot password $mysql_root_pwd",
}

exec {"config ufw":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow mysql",
}

exec {"accept iptables input":
    path    => "/sbin:/usr/share",
    command => "iptables --policy INPUT ACCEPT",
}

exec {"accept iptables output":
    path    => "/sbin:/usr/share",
    command => "iptables --policy OUTPUT ACCEPT",
}

exec {"accept iptables forward":
    path    => "/sbin:/usr/share",
    command => "iptables --policy FORWARD ACCEPT",
}

file {"/etc/rc.local":
    ensure  => present,
    owner   => root,
    group   => root,
    mode    => 755,
    content => "
/etc/init.d/lldpd stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
/etc/init.d/lldpd start >> /home/%(user)s/bcf/%(role)s.log 2>&1
service mysql stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
service mysql start >> /home/%(user)s/bcf/%(role)s.log 2>&1
service cloudstack-management stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
service cloudstack-management start >> /home/%(user)s/bcf/%(role)s.log 2>&1
exit 0
",
}

file {"/etc/mysql/conf.d/cloudstack.cnf":
    ensure  => present,
    owner   => root,
    group   => root,
    mode    => 0644,
    content => "
[mysqld]
innodb_rollback_on_timeout=1
innodb_lock_wait_timeout=600
max_connections=350
log-bin=mysql-bin
binlog-format = 'ROW'
",
    notify  => Service['mysql'],
}

file {"/export":
    ensure => "directory",
    owner  => root,
    group  => root,
    mode   => 0666,
}

file {"/export/primary":
    ensure  => "directory",
    owner   => root,
    group   => root,
    mode    => 0666,
    require => File['/export'],
}

file {"/export/secondary":
    ensure  => "directory",
    owner   => root,
    group   => root,
    mode    => 0666,
    require => File['/export'],
}


file {'/etc/exports':
    ensure  => present,
}

file_line {'config primary':
    path    => '/etc/exports',  
    line    => '/export/primary *(rw,async,no_root_squash,no_subtree_check)',
    match   => "^/export/primary.*$",
    require => File['/etc/exports'],
}

file_line {'config secondary':
    path    => '/etc/exports',
    line    => '/export/secondary *(rw,async,no_root_squash,no_subtree_check)',
    match   => "^/export/secondary.*$",
    require => File['/etc/exports'],
}


service {'mysql':
    ensure  => running,
    require => Package["mysql-server"],
}

exec {'export nfs':
    require     => Package['nfs-kernel-server'],
    subscribe   => [File_Line["config primary"],
                    File_Line["config secondary"]],
    refreshonly => true,
    path        => "/bin:/usr/bin:/usr/sbin:/sbin",
    command     => "exportfs -a",
}

exec {"install maven3":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "apt-get install -fy --force-yes maven3",
    require => Exec['update jdk'],
    returns => [0, 100],
}

exec {"dpkg common":
    require => [Exec["install maven3"],
                Exec['export nfs'],
                Package['tomcat6'],
                Package['jsvc'],
                Package['qemu-kvm'],
                Package['ubuntu-vm-builder'],
                Package['mysql-server'],
                Package['ethtool'],
                Package['mysql-client'],
                Package['openjdk-7-jdk'],
                Package['python-software-properties'],
                Package['debhelper'],
                Package['genisoimage'],
                Package['python-mysqldb'],
                Package['augeas-lenses'],
                Package['libaugeas0'],
                Package['libcommons-daemon-java'],
                Package['libmysql-java'],
                Package['python-paramiko'],
                Package['augeas-tools']],
    user    => root,
    path    => "/bin:/usr/bin:/usr/sbin:/sbin",
    command => "dpkg -i /home/$user/bcf/$cs_common",
    returns => [0],
}

exec {"dpkg management":
    require => Exec['dpkg common'],
    user    => root,
    path    => "/bin:/usr/bin:/usr/sbin:/sbin",
    command => "dpkg -i /home/$user/bcf/$cs_mgmt",
    returns => [0],
}

exec {"install cloudstack":
    require => Exec['dpkg management'],
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "apt-get -fy install",
}

service {"dbus":
    require => Package['dbus'],
    ensure  => running,
    enable  => true,
}

service {"tomcat6":
    require => Package['tomcat6'],
    ensure  => running,
    enable  => true,
}
'''

# compute node puppet template
COMPUTE_PUPPET = r'''

$user       = "%(user)s"
$distro     = 'precise'
$cs_url     = "%(cs_url)s"
$cs_common  = "%(cs_common)s"
$cs_agent   = "%(cs_agent)s"

class {'apt':
    always_apt_update => true,
}

file_line {'backports_deb':
    path    => '/etc/apt/sources.list',
    line    => "deb http://ppa.launchpad.net/pfak/backports/ubuntu ${distro} main",
    match   => "^deb http://ppa.launchpad.net/pfak/backports/ubuntu.*$",
    require => File['/etc/apt/sources.list'],
}

file_line {'backports_src_deb':
    path    => '/etc/apt/sources.list',
    line    => "deb-src http://ppa.launchpad.net/pfak/backports/ubuntu ${distro} main",
    match   => "^deb-src http://ppa.launchpad.net/pfak/backports/ubuntu.*$",
    require => File['/etc/apt/sources.list'],
}

exec {"update":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "apt-get update",
    require => [File_Line['backports_deb'],
                File_line['backports_src_deb']],
    returns => [0, 100],
}

package {[
    'ethtool',
    'dbus',
    'qemu-kvm',
    'ubuntu-vm-builder',
    'openjdk-7-jre',
    'libcommons-daemon-java',
    'jsvc',
    'ipset',
    'python-software-properties',
    'nfs-common',
    'aptitude',
    'genisoimage',
    ]:
    ensure  => 'installed',
    require => Exec['update'],
    notify  => Service['dbus'],
}->

file {'/etc/libvirt/qemu.conf':
    ensure  => present,
}

file_line {'config user':
    path    => '/etc/libvirt/qemu.conf',  
    line    => "user=\"root\"",
    match   => "^user=.*$",
    require => File['/etc/libvirt/qemu.conf'],
}

file_line {'config group':
    path    => '/etc/libvirt/qemu.conf',
    line    => "group=\"root\"",
    match   => "^group=.*$",
    require => File['/etc/libvirt/qemu.conf'],
}

file {"/etc/rc.local":
    ensure  => present,
    owner   => root,
    group   => root,
    mode    => 755,
    content => "
sleep 30
route del default
route add default gw %(pxe_gw)s
/etc/init.d/lldpd stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
/etc/init.d/lldpd start >> /home/%(user)s/bcf/%(role)s.log 2>&1
service dbus stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
service dbus start >> /home/%(user)s/bcf/%(role)s.log 2>&1
service libvirt-bin stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
service libvirt-bin start >> /home/%(user)s/bcf/%(role)s.log 2>&1
service cloudstack-agent stop >> /home/%(user)s/bcf/%(role)s.log 2>&1
service cloudstack-agent start >> /home/%(user)s/bcf/%(role)s.log 2>&1
exit 0
",
}

exec {"allow tcp 22":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow proto tcp from any to any port 22",
}

exec {"allow tcp 1798":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow proto tcp from any to any port 1798",
}

exec {"allow tcp 16509":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow proto tcp from any to any port 16509",
}

exec {"allow tcp 5900:6100":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow proto tcp from any to any port 5900:6100",
}

exec {"allow tcp 49152:49216":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "ufw allow proto tcp from any to any port 49152:49216",
}

exec {"accept iptables input":
    path    => "/sbin:/usr/share",
    command => "iptables --policy INPUT ACCEPT",
}

exec {"accept iptables output":
    path    => "/sbin:/usr/share",
    command => "iptables --policy OUTPUT ACCEPT",
}

exec {"accept iptables forward":
    path    => "/sbin:/usr/share",
    command => "iptables --policy FORWARD ACCEPT",
}

service {"libvirt-bin":
    enable  => true,
    ensure  => running,
    require => [File_Line['config user'],
                File_Line['config group'],
                Service['dbus']],
}

exec {"dpkg common":
    require => [Package['ethtool'],
                Package['qemu-kvm'],
                Package['ubuntu-vm-builder'],
                Package['openjdk-7-jre'],
                Package['libcommons-daemon-java'],
                Package['jsvc'],
                Package['ipset'],
                Package['python-software-properties'],
                Package['nfs-common'],
                Package['aptitude'],
                Package['genisoimage'],
                Service["libvirt-bin"]],
    user    => root,
    path    => "/bin:/usr/bin:/usr/sbin:/sbin",
    command => "dpkg -i /home/$user/bcf/$cs_common",
    returns => [0],
}

exec {"dpkg agent":
    require => Exec['dpkg common'],
    user    => root,
    path    => "/bin:/usr/bin:/usr/sbin:/sbin",
    command => "dpkg -i /home/$user/bcf/$cs_agent",
    returns => [0],
}

exec {"install cloudstack":
    require => Exec['dpkg agent'],
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "apt-get -fy install",
}

service {"dbus":
    require => Package['dbus'],
    ensure  => running,
    enable  => true,
    notify  => Service['libvirt-bin'],
}

service {"cloudstack-agent":
    require => [Exec['install cloudstack'],
                Service['dbus']],
    enable  => true,
}
'''

LLDP_PUPPET = r'''
$bond_interfaces = '%(bond_interfaces)s'

file {"/etc/default/lldpd" :
    require => Exec['rm -f /var/run/lldpd.socket'],
    ensure  => present,
    owner   => root,
    group   => root,
    mode    => 0644,
    content => "DAEMON_ARGS='-S 5c:16:c7:00:00:00 -I ${bond_interfaces}'\n",
    notify  => Service['lldpd'],
}

file {'/etc/modules':
    ensure  => present,
}

file_line {'config bonding':
    path    => '/etc/modules',
    line    => "bonding",
    match   => "^bonding$",
    require => File['/etc/modules'],
}

file_line {'config vlan':
    path    => '/etc/modules',
    line    => "8021q",
    match   => "^8021q$",
    require => File['/etc/modules'],
}

file_line {'config loop':
    path    => '/etc/modules',
    line    => "loop",
    match   => "^loop$",
    require => File['/etc/modules'],
}

package {["lldpd", "vlan", "ifenslave-2.6"]:
    ensure => installed,
}

exec {'rm -f /var/run/lldpd.socket':
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "rm -rf /var/run/lldpd.socket",
    require => Package[lldpd],
}

exec {"start lldpd":
    path    => "/bin:/usr/bin:/usr/sbin",
    command => "/etc/init.d/lldpd start",
    require => [Package['lldpd'],
                File['/etc/default/lldpd']],
}

service {"lldpd":
    ensure  => running,
    enable  => true,
    require => Exec['start lldpd'],
}
'''

DB_BASH = r'''
#!/bin/bash
mysql -uroot -p%(mysql_root_pwd)s -e "DROP DATABASE cloud; DROP DATABASE cloud_usage; DROP USER cloud@localhost;"
cloudstack-setup-databases cloud:%(cloud_db_pwd)s@localhost --deploy-as=root:%(mysql_root_pwd)s -i %(hostname)s
'''

NODE_REMOTE_BASH = r'''
#!/bin/bash
COMPUTE_OS="%(COMPUTE_OS)s"
if [[ ("${COMPUTE_OS}" == "ubuntu") || ("%(role)s" == "management") ]]; then
    cp /home/%(user)s/bcf/%(role)s.intf /etc/network/interfaces
    apt-get install -fy puppet aptitude --force-yes
    wget http://apt.puppetlabs.com/puppetlabs-release-precise.deb -O /home/%(user)s/bcf/puppetlabs-release-precise.deb
    dpkg -i /home/%(user)s/bcf/puppetlabs-release-precise.deb
    apt-get update
    puppet resource package puppet ensure=latest
    apt-get install -fy qemu-kvm libvirt-bin ubuntu-vm-builder bridge-utils
    adduser `id -un` libvirtd
    version="$(virsh --version)"
    if [[ "$version" < "1.0.2" ]]; then
        apt-get install -fy python-software-properties
        add-apt-repository ppa:pfak/backports -y
        apt-get update -fy
        aptitude update -fy
        aptitude -fy safe-upgrade
    fi
    service dbus stop
    service dbus start
    service libvirt-bin stop
    service libvirt-bin start
    apt-get -fy install --fix-missing
    puppet module install puppetlabs-apt --force
    puppet module install puppetlabs-stdlib --force
    puppet apply -d -v -l /home/%(user)s/bcf/%(role)s.log /home/%(user)s/bcf/%(role)s.pp
    DEBIAN_FRONTEND=noninteractive aptitude install -y -q iptables-persistent
    apt-get -fy install --fix-missing
    role="%(role)s"
    if [[ "$role" == "management" ]]; then
        service cloudstack-management stop
        cloudstack-setup-databases cloud:%(cloud_db_pwd)s@localhost --deploy-as=root:%(mysql_root_pwd)s -i %(hostname)s
        service mysql stop
        service mysql start
        cloudstack-setup-management   
        service cloudstack-management start
        sleep 300
    else
        reboot
    fi
else
    host_name_label="%(host_name_label)s"
    network_name_labels=%(network_name_labels)s
    vlan_tags=%(vlan_tags)s
    bond_intfs=%(bond_intfs)s
    bond_inets=%(bond_inets)s
    bond_ips=%(bond_ips)s
    bond_masks=%(bond_masks)s
    bond_gateways=%(bond_gateways)s
    user_name="%(user)s"
    pxe_intf="%(pxe_intf)s"
    pxe_inet="%(pxe_inet)s"
    pxe_address="%(pxe_address)s"
    pxe_netmask="%(pxe_netmask)s"
    pxe_gw="%(pxe_gw)s"
    pxe_dns="%(pxe_dns)s"

    export PATH="/sbin:/opt/xensource/bin:$PATH"

    # wget vhd-util
    mkdir -p /opt/cloud/bin
    cp /home/${user_name}/bcf/vhd-util /opt/cloud/bin/
    chmod 777 /opt/cloud/bin/vhd-util
    mkdir -p /opt/xensource/bin
    cp /home/${user_name}/bcf/vhd-util /opt/xensource/bin/
    chmod 777 /opt/xensource/bin/vhd-util

    # install lldp
    yum install -y /home/${user_name}/bcf/lm_sensors-2.10.7-9.el5.i386.rpm
    yum install -y lldpd

    # configure NTP
    yum install -y ntp
    sed -i '/xenserver.pool.ntp.org/d' /etc/ntp.conf
    sed -i '/0.bigswitch.pool.ntp.org/d' /etc/ntp.conf
    echo '0.bigswitch.pool.ntp.org' >> /etc/ntp.conf
    /sbin/service ntpd restart
    /sbin/chkconfig --add ntpd
    /sbin/chkconfig ntpd on

    # disable iptables
    /sbin/service iptables stop

    # configure bond
    host_uuid="$(xe host-list | grep -w ${host_name_label} -B1 | grep -w uuid | awk '{print $NF}')"
    bond_intf_uuids=()
    for bond_intf in ${bond_intfs[@]}; do
        bond_intf_uuid=$(xe pif-list params=all | grep -w "${host_name_label}" -B15 | grep -w "${bond_intf}" -B1 | grep -w uuid | grep -v network | awk '{print $NF}')
        bond_intf_uuids=("${bond_intf_uuids[@]}" "$bond_intf_uuid")
    done

    # configure management network
    bond_uuid=''
    bond_pif_uuid=''
    bond_bridge=''
    count=${#vlan_tags[@]}
    for (( i=0; i<${count}; i++ )); do
        network_name_label=${network_name_labels[$i]}
        vlan_tag=${vlan_tags[$i]}
        bond_inet=${bond_inets[$i]}
        bond_ip=${bond_ips[$i]}
        bond_mask=${bond_masks[$i]}
        bond_gateway=${bond_gateways[$i]}

        if [[ ${vlan_tag} == '' ]]; then
            network_uuid="$(xe network-create name-label=${network_name_label})"
            pif_uuids=$(IFS=, ; echo "${bond_intf_uuids[*]}")
            bond_uuid=$(xe bond-create network-uuid=${network_uuid} pif-uuids=${pif_uuids} mode=active-backup)
            bond_bridge=$(xe network-list params=all | grep -w ${network_uuid} -A6 | grep -w bridge | awk '{print $NF}')
            bond_pif_uuid=$(xe pif-list params=all | grep -w "${host_name_label}" -B15 | grep -w "${network_name_label}" -B13 | grep -w "VLAN ( RO): -1" -B6 | grep bond -B1 | grep -w uuid | grep -v network | awk '{print $NF}')

            # configure ip address to bond interface
            if [[ ${bond_inet} == 'static' ]]; then
                xe pif-reconfigure-ip uuid=${bond_pif_uuid} mode=${bond_inet} IP=${bond_ip} netmask=${bond_mask} gateway=${bond_gateway}
                ping ${bond_gateway} -c3
            else
                xe pif-reconfigure-ip uuid=${bond_pif_uuid} mode=${bond_inet}
            fi
            break
        fi
    done

    if [[ ${bond_uuid} == '' ]]; then
        echo 'Error: fails to create bond'
        exit 1
    fi

    bond_name=$(xe pif-list params=all | grep -w ${host_name_label} -B14 | grep -w ${bond_uuid} -B6 | grep -w device | awk '{print $NF}')
    echo "host name: ${host_name_label}, bond bridge: ${bond_bridge}, bond: ${bond_name}"

    # configure vlan
    for (( i=0; i<${count}; i++ )); do
        network_name_label=${network_name_labels[$i]}
        vlan_tag=${vlan_tags[$i]}
        bond_inet=${bond_inets[$i]}
        bond_ip=${bond_ips[$i]}
        bond_mask=${bond_masks[$i]}
        bond_gateway=${bond_gateways[$i]}

        if [[ ${vlan_tag} == '' ]]; then
            continue
        fi

        network_uuid="$(xe network-create name-label=${network_name_label})"
        vlan_uuid=$(xe vlan-create network-uuid=${network_uuid} pif-uuid=${bond_pif_uuid} vlan=${vlan_tag})
        pif_uuid=$(xe pif-list params=all | grep -w "${host_name_label}" -B15 | grep -w "${network_name_label}" -B13 | grep -w "${vlan_tag}" -B6 | grep bond -B1 | grep -w uuid | grep -v network | awk '{print $NF}')
        if [[ ${bond_inet} == 'static' ]]; then
            xe pif-reconfigure-ip uuid=${pif_uuid} mode=${bond_inet} IP=${bond_ip} netmask=${bond_mask} gateway=${bond_gateway}
            ping ${bond_gateway} -c3
        else
            xe pif-reconfigure-ip uuid=${pif_uuid} mode=${bond_inet}
        fi

        bridge=$(xe network-list | grep -w ${network_uuid} -A3 | grep -w bridge | awk '{print $NF}')
        echo "host name: ${host_name_label}, vlan: ${vlan_tag}, bridge: ${bridge}"
    done

    # configure pxe interface
    pif_uuid=$(xe pif-list params=all | grep -w ${host_name_label} -B15 | grep -w ${pxe_intf} -B1 | grep -w uuid | grep -v network | awk '{print $NF}')
    if [[ ${pxe_inet} == 'static' ]]; then
        xe pif-reconfigure-ip uuid=${pif_uuid} mode=${pxe_inet} IP=${pxe_address} netmask=${pxe_netmask} gateway=${pxe_gw} DNS=${pxe_dns}
    else
        xe pif-reconfigure-ip uuid=${pif_uuid} mode=${pxe_inet}
    fi

    # use linux bridge instead of ovs
    /opt/xensource/bin/xe-switch-network-backend bridge

    # change default gw on upstart script
    echo "sleep 60" >> /etc/rc.local
    echo "route del default" >> /etc/rc.local
    echo "route add default gw ${pxe_gw}" >> /etc/rc.local

    echo "/sbin/service iptables stop" >> /etc/rc.local

fi
'''

XEN_SLAVE = r'''
#!/bin/bash

host_name_label="%(host_name_label)s"
master_address="%(master_address)s"
master_username="%(master_username)s"
master_pwd="%(master_pwd)s"
user_name="%(username)s"
bond_intfs=%(bond_intfs)s
pxe_gw="%(pxe_gw)s"

export PATH="/sbin:/opt/xensource/bin:$PATH"

# prepare vhd-util
mkdir -p /opt/cloud/bin
cp /home/${user_name}/bcf/vhd-util /opt/cloud/bin/
chmod 777 /opt/cloud/bin/vhd-util
mkdir -p /opt/xensource/bin
cp /home/${user_name}/bcf/vhd-util /opt/xensource/bin
chmod 777 /opt/xensource/bin/vhd-util

# install lldp
yum install -y /home/${user_name}/bcf/lm_sensors-2.10.7-9.el5.i386.rpm
yum install -y lldpd

# configure NTP
yum install -y ntp
sed -i '/xenserver.pool.ntp.org/d' /etc/ntp.conf
sed -i '/0.bigswitch.pool.ntp.org/d' /etc/ntp.conf
echo '0.bigswitch.pool.ntp.org' >> /etc/ntp.conf
/sbin/service ntpd restart
/sbin/chkconfig --add ntpd
/sbin/chkconfig ntpd on

# disable iptables
/sbin/service iptables stop

# use linux bridge instead of ovs
/opt/xensource/bin/xe-switch-network-backend bridge

echo "${host_name_label}" "starts to join pool with master:" "${master_address}"
xe pool-join master-address=${master_address} master-username=${master_username} master-password=${master_pwd} force=true
echo "${host_name_label}" "finishes joining pool with master:" "${master_address}"

# change default gw on upstart script
echo "sleep 60" >> /etc/rc.local
echo "route del default" >> /etc/rc.local
echo "route add default gw ${pxe_gw}" >> /etc/rc.local

echo "/sbin/service iptables stop" >> /etc/rc.local

'''

XEN_IP_ASSIGNMENT=r'''
#!/bin/bash

user_name="%(username)s"
xenserver_pool="%(xenserver_pool)s"
cluster_size=%(cluster_size)d
count_down=30
slave_name_labels=%(slave_name_labels)s
bond_vlans=%(bond_vlans)s
bond_inets=%(bond_inets)s
bond_ips=%(bond_ips)s
bond_masks=%(bond_masks)s
bond_gateways=%(bond_gateways)s

export PATH="/sbin:/opt/xensource/bin:$PATH"

# wait at most 30 seconds for all slaves to join cluster
count=${count_down}
hosts_online=$(xe host-list | grep -w uuid | wc -l)
while [[ ${count} > 0 ]] && [[ ${hosts_online} < ${cluster_size} ]]; do
    hosts_online=$(xe host-list | grep -w uuid | wc -l)
    let count-=1
    sleep 1
done
echo "Pool" "${xenserver_pool}" "has" "${hosts_online}" "compute nodes online"

# configure bonds to all nodes
# wait at most 120 seconds for all networks
count=${count_down}
while [[ ${count} > 0 ]]; do
    bash /home/${user_name}/bcf/cloud-setup-bonding.sh
    if [[ $? == 0 ]]; then
        break
    fi
    let count-=1
    sleep 4
done

# configure bond interface ip
ip_array=()
bond_count=${#bond_inets[@]}
slave_count=${#slave_name_labels[@]}
for (( i=0; i<${slave_count}; i++ )); do
    slave_name_label=${slave_name_labels[$i]}
    existence=$(xe host-list | grep ${slave_name_label} | wc -l)
    if [[ $existence == 0 ]]; then
        continue
    fi
    let start_index=i*bond_count
    for (( j=0; j<${bond_count}; j++ )); do
        inet=${bond_inets[$j]}
        vlan=${bond_vlans[$j]}
        if [[ $vlan == "" ]]; then
            vlan="-1"
        fi
        let k=start_index+j
        ip=${bond_ips[$k]}
        mask=${bond_masks[$k]}
        gateway=${bond_gateways[$k]}
        pif_uuid=$(xe pif-list host-name-label=${slave_name_label} device-name='' VLAN=${vlan} | grep -w uuid | grep -v network | awk '{print $NF}')
        if [[ ${inet} == 'static' ]]; then
            xe pif-reconfigure-ip uuid=${pif_uuid} mode=${inet} IP=${ip} netmask=${mask} gateway=${gateway}
            ip_array=("${ip_array[@]}" "${ip}")
            ping ${gateway} -c3
        else
            xe pif-reconfigure-ip uuid=${pif_uuid} mode=${inet}
        fi
    done
done

ip_count=${#ip_array[@]}
count_down=60
while [[ ${count_down} > 0 ]]; do
    success_count=0
    for (( i=0; i<${ip_count}; i++ )); do
        ip=${ip_array[$i]}
        ping ${ip} -c1
        if [[ $? == 0 ]]; then
            let success_count+=1
        fi
    done
    if [[ ${ip_count}==${success_count} ]]; then
        break
    fi
    let count_down-=1
    if [[ ${count_down}==0 ]]; then
        echo "Failed to assign IP:" "${ip}"
    fi
done
'''

XEN_CHANGE_MGMT_INTF=r'''
#!/bin/bash
host_name_label="%(host_name_label)s"

export PATH="/sbin:/opt/xensource/bin:$PATH"

# change management interface to bond
network_uuid=$(xe pif-list host-name-label=${host_name_label} device-name='' VLAN=-1 params=all | grep -w network-uuid | awk '{print $NF}')

# configure lldp
bond_name=$(xe pif-list host-name-label=${host_name_label} device-name='' VLAN=-1 params=all | grep -w device | grep bond | awk '{print $NF}')
sed -i '/LLDPD_OPTIONS/d' /etc/sysconfig/lldpd
echo "LLDPD_OPTIONS=\"-S 5c:16:c7:00:00:00 -I ${bond_name}\"" >> /etc/sysconfig/lldpd
/sbin/chkconfig --add lldpd
/sbin/chkconfig lldpd on
/sbin/service lldpd start

mgmt_bridge=$(xe network-param-get param-name=bridge uuid=${network_uuid})
sed -i "/^MANAGEMENT_INTERFACE=/s/=.*/=\'${mgmt_bridge}\'/" /etc/xensource-inventory
echo "host name: ${host_name_label}, management bridge: ${mgmt_bridge}, management bond: ${bond_name}"
/opt/xensource/bin/xe-switch-network-backend bridge
/opt/xensource/bin/xe-toolstack-restart
'''

XEN_SLAVE_REBOOT=r'''
#!/bin/bash
master_address="%(master_address)s"
count_down=300 
while [[ ${count_down} > 0 ]]; do
    echo quit | telnet ${master_address} 22 2>/dev/null | grep Connected
    if [[ $? == 0 ]]; then
        break
    fi
    let count_down-=1
    sleep 1
done
reboot
'''

XEN_CHECK_BOND=r'''
#!/bin/bash

count_down=300
while [[ ${count_down} > 0 ]]; do
    echo quit | telnet %(hostname)s 22 2>/dev/null | grep Connected
    if [[ $? == 0 ]]; then
        sleep 180
        intf_count=$(sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s "echo %(pwd)s | sudo -S cat /proc/net/bonding/bond0 | grep -w Interface | wc -l")
        echo "%(hostname)s has ${intf_count/$'\r'/} interfaces in bond0" >> %(log)s
        if [[ ${intf_count/$'\r'/} == %(intf_count)d ]]; then
            exit 0
        fi
        break
    fi
    let count_down-=1
    sleep 1
done
echo "ERROR BOND ON " "%(hostname)s" >> %(log)s
exit 1
'''

NODE_LOCAL_BASH = r'''
#!/bin/bash

echo -e "Start to deploy %(role)s node %(hostname)s...\n"
sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S mkdir -m 0777 -p /home/%(user)s/bcf"
if [[ ("%(role)s" == "management") || ("%(COMPUTE_OS)s" == "ubuntu") ]]; then
    echo -e "Copy /etc/network/interfaces to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(hostname)s.intf %(user)s@%(hostname)s:/home/%(user)s/bcf/%(role)s.intf >> %(log)s 2>&1
    echo -e "Copy %(role)s.pp to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(hostname)s.pp %(user)s@%(hostname)s:/home/%(user)s/bcf/%(role)s.pp >> %(log)s 2>&1
    echo -e "Copy %(CS_COMMON)s to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(CS_COMMON)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_COMMON)s >> %(log)s 2>&1
    if [ -f /tmp/%(hostname)s.db.sh ]; then
        echo -e "Copy db.sh to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(hostname)s.db.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/db.sh >> %(log)s 2>&1
        echo -e "Copy %(CS_MGMT)s to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(CS_MGMT)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_MGMT)s >> %(log)s 2>&1
    else
        echo -e "Copy %(CS_AGENT)s to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(CS_AGENT)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_AGENT)s >> %(log)s 2>&1
    fi
    echo -e "Copy %(role)s.sh to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(hostname)s.remote.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/%(role)s.sh >> %(log)s 2>&1
    echo -e "Run %(role)s.sh on node %(hostname)s\n"
    echo -e "Open another command prompt and use \"tail -f %(log)s\" to display the progress\n"
    sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/%(role)s.sh"
    echo -e "Finish deploying %(role)s on %(hostname)s\n"
fi
if [[ ("%(COMPUTE_OS)s" == "xenserver") && ("%(role)s" == "compute") ]]; then
    if [[ ! -f /tmp/vhd-util ]]; then
        wget http://download.cloud.com.s3.amazonaws.com/tools/vhd-util -P /tmp/
    fi
    echo -e "Copy vhd-util to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/vhd-util %(user)s@%(hostname)s:/home/%(user)s/bcf/ >> %(log)s 2>&1

    if [[ ! -f /tmp/lm_sensors-2.10.7-9.el5.i386.rpm ]]; then
        wget ftp://rpmfind.net/linux/centos/5.11/os/i386/CentOS/lm_sensors-2.10.7-9.el5.i386.rpm -P /tmp/
    fi
    echo -e "Copy lm_sensors-2.10.7-9.el5.i386.rpm to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/lm_sensors-2.10.7-9.el5.i386.rpm %(user)s@%(hostname)s:/home/%(user)s/bcf/ >> %(log)s 2>&1

    if [[ ! -f /tmp/home:vbernat.repo ]]; then
        wget http://download.opensuse.org/repositories/home:vbernat/CentOS_5/home:vbernat.repo -P /tmp/
    fi
    echo -e "Copy home:vbernat.repo to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/home:vbernat.repo %(user)s@%(hostname)s:/etc/yum.repos.d/ >> %(log)s 2>&1

    if [[ ! -f /tmp/cloud-setup-bonding.sh ]]; then
        wget --no-check-certificate https://raw.githubusercontent.com/apache/cloudstack/master/scripts/vm/COMPUTE_OS/xenserver/cloud-setup-bonding.sh -P /tmp/
    fi
    echo -e "Copy cloud-setup-bonding.sh to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/cloud-setup-bonding.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/ >> %(log)s 2>&1

    echo -e "Copy mgmtintf.sh to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(hostname)s.mgmtintf.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/mgmtintf.sh >> %(log)s 2>&1
    if [[ ! -f /tmp/%(hostname)s.%(pool)s.bondip.sh ]]; then
        echo -e "Copy slave.sh to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(hostname)s.slave.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/slave.sh >> %(log)s 2>&1
        echo -e "Copy slave_reboot.sh to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(hostname)s.slave_reboot.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/slave_reboot.sh >> %(log)s 2>&1
    else
        echo -e "Copy bondip.sh to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(hostname)s.%(pool)s.bondip.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/bondip.sh >> %(log)s 2>&1
        echo -e "Copy %(role)s.sh to node %(hostname)s\n"
        sshpass -p %(pwd)s scp /tmp/%(hostname)s.remote.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/%(role)s.sh >> %(log)s 2>&1
        echo -e "Run %(role)s.sh on node %(hostname)s\n"
        echo -e "Open another command prompt and use \"tail -f %(log)s\" to display the progress\n"
        sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/%(role)s.sh"
        echo -e "Finish deploying master %(role)s on %(pool)s %(hostname)s\n"
    fi
fi
'''

CENTOS_ETH=r'''
DEVICE=%(device)s
MASTER=%(bond_name)s
SLAVE=yes
USERCTL=no
ONBOOT=yes
BOOTPROTO=none
NM_CONTROLLED=no
'''

CENTOS_BASE_BOND=r'''
DEVICE=%(bond_name)s
BOOTPROTO=none
ONBOOT=yes
USERCTL=no
BONDING_OPTS="mode=0 miimon=50 updelay=15000"
NM_CONTROLLED=no
'''

CENTOS_BASE_BRIDGE_BOND=r'''
DEVICE=%(bond_name)s
BOOTPROTO=none
ONBOOT=yes
USERCTL=no
BONDING_OPTS="mode=0 miimon=50 updelay=15000"
BRIDGE=%(bridge_name)s
NM_CONTROLLED=no
'''

CENTOS_TAGGED_BRIDGE_BOND=r'''
DEVICE=%(bond_name)s.%(vlan)d
BOOTPROTO=none
ONBOOT=yes
USERCTL=no
VLAN=yes
BRIDGE=%(bridge_name)s
NM_CONTROLLED=no
'''

CENTOS_STATIC_BRIDGE=r'''
DEVICE=%(bridge_name)s
TYPE=Bridge
ONBOOT=yes
BOOTPROTO=none
IPV6INIT=no
IPV6_AUTOCONF=no
DELAY=5
IPADDR=%(address)s
NETWORK=%(network)s
NETMASK=%(netmask)s
GATEWAY=%(gateway)s
NM_CONTROLLED=no
'''

CENTOS_DHCP_BRIDGE=r'''
DEVICE=%(bridge_name)s
TYPE=Bridge
ONBOOT=yes
BOOTPROTO=dhcp
IPV6INIT=no
IPV6_AUTOCONF=no
DELAY=5
NM_CONTROLLED=no
'''

CENTOS_BASE_STATIC_BOND=r'''
DEVICE=%(bond_name)s
BOOTPROTO=none
ONBOOT=yes
USERCTL=no
BONDING_OPTS="mode=0 miimon=50 updelay=15000"
IPADDR=%(address)s
NETWORK=%(network)s
NETMASK=%(netmask)s
NM_CONTROLLED=no
'''

CENTOS_BASE_DHCP_BOND=r'''
DEVICE=%(bond_name)s
BOOTPROTO=dhcp
ONBOOT=yes
USERCTL=no
BONDING_OPTS="mode=0 miimon=50 updelay=15000"
NM_CONTROLLED=no
'''

CENTOS_BOND_ALIAS=r'''
alias %(bond_name)s bonding
'''

CENTOS_TAGGED_STATIC_BOND=r'''
DEVICE=%(bond_name)s.%(vlan)d
BOOTPROTO=none
ONBOOT=yes
USERCTL=no
IPADDR=%(address)s
NETWORK=%(network)s
NETMASK=%(netmask)s
VLAN=yes
NM_CONTROLLED=no
'''

CENTOS_TAGGED_DHCP_BOND=r'''
DEVICE=%(bond_name)s.%(vlan)d
BOOTPROTO=dhcp
ONBOOT=yes
USERCTL=no
VLAN=yes
NM_CONTROLLED=no
'''

CENTOS_MGMT_REMOTE=r'''
#!/bin/bash

bond_intfs="%(bond_intfs)s"

# install and config lldp
cd /etc/yum.repos.d/;
rm -f /etc/yum.repos.d/home:vbernat.repo.*
rm -f /etc/yum.repos.d/home:vbernat.repo
wget http://download.opensuse.org/repositories/home:vbernat/CentOS_CentOS-6/home:vbernat.repo;
yum clean all
yum clean metadata
yum update -y
yum -y install lldpd
sed -i '/LLDPD_OPTIONS/d' /etc/sysconfig/lldpd
echo "LLDPD_OPTIONS=\"-S 5c:16:c7:00:00:00 -I ${bond_intfs}\"" >> /etc/sysconfig/lldpd
/sbin/chkconfig --add lldpd
/sbin/chkconfig lldpd on
/sbin/service lldpd stop
/sbin/service lldpd start

# put vhd-util
mkdir -p /opt/cloud/bin
cp /home/%(user)s/bcf/vhd-util /opt/cloud/bin/
chmod 777 /opt/cloud/bin/vhd-util
mkdir -p /opt/xensource/bin
cp /home/%(user)s/bcf/vhd-util /opt/xensource/bin/
chmod 777 /opt/xensource/bin/vhd-util
mkdir -p /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver
cp /home/%(user)s/bcf/vhd-util /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver/
chmod 777 /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver/vhd-util

# install and config NFS
yum -y install nfs*
/sbin/service rpcbind start
/sbin/chkconfig rpcbind on
/sbin/service nfs start
/sbin/chkconfig nfs on
mkdir -p /export/primary
mkdir -p /export/secondary
chmod 755 /export/primary
chmod 755 /export/secondary
sed -i "/primary/d" /etc/exports
sed -i "/secondary/d" /etc/exports
echo "/export/primary *(rw,async,no_root_squash,no_subtree_check)" >> /etc/exports
echo "/export/secondary *(rw,async,no_root_squash,no_subtree_check)" >> /etc/exports
/etc/init.d/nfs restart
/usr/sbin/exportfs -a

# update iptables rules
iptables -F
iptables -A INPUT -p udp --dport 111 -j ACCEPT
iptables -A INPUT -p tcp --dport 111 -j ACCEPT
iptables -A INPUT -p tcp --dport 2049 -j ACCEPT
iptables -A INPUT -p tcp --dport 32803 -j ACCEPT
iptables -A INPUT -p udp --dport 32769 -j ACCEPT
iptables -A INPUT -p tcp --dport 892 -j ACCEPT
iptables -A INPUT -p udp --dport 892 -j ACCEPT
iptables -A INPUT -p tcp --dport 875 -j ACCEPT
iptables -A INPUT -p udp --dport 875 -j ACCEPT
iptables -A INPUT -p tcp --dport 662 -j ACCEPT
iptables -A INPUT -p udp --dport 662 -j ACCEPT
iptables -A INPUT -p tcp --dport 3922 -j ACCEPT
iptables -A INPUT -p tcp --dport 3306 -j ACCEPT
/sbin/service iptables save
/sbin/service iptables restart

# install and config mysql server
yum install -y mysql-server
sed -i "/innodb_rollback_on_timeou/d" /etc/my.cnf
sed -i "/innodb_lock_wait_timeout/d" /etc/my.cnf
sed -i "/max_connections/d" /etc/my.cnf
sed -i "/log-bin/d" /etc/my.cnf
sed -i "/binlog-format/d" /etc/my.cnf
sed -i "/\[mysqld\]/a innodb_rollback_on_timeout=1\ninnodb_lock_wait_timeout=600\nmax_connections=350\nlog-bin=mysql-bin\nbinlog-format=\'ROW\'" /etc/my.cnf
/sbin/chkconfig --add mysqld
/sbin/chkconfig mysqld on
/sbin/service mysqld stop
/sbin/service mysqld start

# install cloudstack dependencies
yum clean all
yum clean metadata
yum update -y
yum install -y --skip-broken java-1.7.0-openjdk-devel.x86_64
yum install -y --skip-broken MySQL-python
yum install -y --skip-broken mysql-connector-java
yum install -y --skip-broken ipmitool
yum install -y --skip-broken mkisofs
yum install -y --skip-broken python-paramiko
yum install -y --skip-broken ws-commons-util
yum install -y --skip-broken tomcat6 
yum clean all
yum clean metadata
yum update -y

# install cloudstack rpm
/bin/rpm -Uvh /home/%(user)s/bcf/cloudstack-common-4.5.0-SNAPSHOT.el6.x86_64.rpm
/bin/rpm -Uvh /home/%(user)s/bcf/cloudstack-management-4.5.0-SNAPSHOT.el6.x86_64.rpm /home/%(user)s/bcf/cloudstack-awsapi-4.5.0-SNAPSHOT.el6.x86_64.rpm

# set up cloudstack db
/usr/bin/mysqladmin -uroot password %(mysql_root_pwd)s
mysql -uroot -p%(mysql_root_pwd)s -e "DROP DATABASE cloud; DROP DATABASE cloud_usage; DROP USER cloud@localhost;"
cloudstack-setup-databases cloud:%(cloud_db_pwd)s@localhost --deploy-as=root:%(mysql_root_pwd)s -i %(hostname)s
/sbin/service mysqld stop
/sbin/service mysqld start

# configure rc.local
sed -i '/lldp/d' /etc/rc.d/rc.local
sed -i '/mysql/d' /etc/rc.d/rc.local
sed -i '/tomcat6/d' /etc/rc.d/rc.local
sed -i '/cloudstack/d' /etc/rc.d/rc.local
sed -i '/exit/d' /etc/rc.d/rc.local
sed -i '/default/d' /etc/rc.d/rc.local
echo "/etc/init.d/lldpd stop >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/etc/init.d/lldpd start >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service mysqld stop >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service mysqld start >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service tomcat6 stop >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service tomcat6 start >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service cloudstack-management stop >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service cloudstack-management start >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "route del default" >> /etc/rc.d/rc.local
echo "route add default gw %(pxe_gw)s" >> /etc/rc.d/rc.local
echo "exit 0" >> /etc/rc.d/rc.local
chmod 777 /etc/rc.d/rc.local

# setup cloudstack management
cloudstack-setup-management
/sbin/chkconfig --add cloudstack-management
/sbin/chkconfig cloudstack-management on
/sbin/service cloudstack-management stop
/sbin/service cloudstack-management start
sleep 300
'''

CENTOS_COMPUTE_REMOTE=r'''
#!/bin/bash

bond_intfs="%(bond_intfs)s"

yum clean all
yum clean metadata
yum update -y

yum install -y --skip-broken ntp
yum install -y --skip-broken kvm
yum install -y --skip-broken libvirt
yum install -y --skip-broken python-virtinst
yum install -y --skip-broken qemu-kvm

# put vhd-util
mkdir -p /opt/cloud/bin
cp /home/%(user)s/bcf/vhd-util /opt/cloud/bin/
chmod 777 /opt/cloud/bin/vhd-util
mkdir -p /opt/xensource/bin
cp /home/%(user)s/bcf/vhd-util /opt/xensource/bin/
chmod 777 /opt/xensource/bin/vhd-util
mkdir -p /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver
cp /home/%(user)s/bcf/vhd-util /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver/
chmod 777 /usr/share/cloudstack-common/scripts/vm/hypervisor/xenserver/vhd-util

# config libvirt
sed -i '/listen_tls/d' /etc/libvirt/libvirtd.conf
sed -i '/listen_tcp/d' /etc/libvirt/libvirtd.conf
sed -i '/tcp_port/d' /etc/libvirt/libvirtd.conf
sed -i '/auth_tcp/d' /etc/libvirt/libvirtd.conf
sed -i '/mdns_adv/d' /etc/libvirt/libvirtd.conf
echo "listen_tls = 0" >> /etc/libvirt/libvirtd.conf
echo "listen_tcp = 1" >> /etc/libvirt/libvirtd.conf
echo "tcp_port = \"16059\"" >> /etc/libvirt/libvirtd.conf
echo "auth_tcp = \"none\"" >> /etc/libvirt/libvirtd.conf
echo "mdns_adv = 0" >> /etc/libvirt/libvirtd.conf

sed -i '//d' /etc/sysconfig/libvirtd
echo "LIBVIRTD_ARGS=\"--listen\"" >> /etc/sysconfig/libvirtd
/sbin/chkconfig --add libvirtd
/sbin/chkconfig libvirtd on
/sbin/service libvirtd restart

# install and config lldp
cd /etc/yum.repos.d/;
rm -f /etc/yum.repos.d/home:vbernat.repo.*
rm -f /etc/yum.repos.d/home:vbernat.repo
wget http://download.opensuse.org/repositories/home:vbernat/CentOS_CentOS-6/home:vbernat.repo;
yum clean all
yum clean metadata
yum update -y
yum install -y --skip-broken lldpd
sed -i '/LLDPD_OPTIONS/d' /etc/sysconfig/lldpd
echo "LLDPD_OPTIONS=\"-S 5c:16:c7:00:00:00 -I ${bond_intfs}\"" >> /etc/sysconfig/lldpd
/sbin/chkconfig --add lldpd
/sbin/chkconfig lldpd on
/sbin/service lldpd stop
/sbin/service lldpd start

# update iptables rules
iptables -F
iptables -I INPUT -p tcp -m tcp --dport 22 -j ACCEPT
iptables -I INPUT -p tcp -m tcp --dport 1798 -j ACCEPT
iptables -I INPUT -p tcp -m tcp --dport 16509 -j ACCEPT
iptables -I INPUT -p tcp -m tcp --dport 5900:6100 -j ACCEPT
iptables -I INPUT -p tcp -m tcp --dport 49152:49216 -j ACCEPT
/sbin/service iptables save
/sbin/service iptables restart

yum clean all
yum clean metadata
yum update -y

yum install -y --skip-broken ipset
yum install -y --skip-broken java7
yum install -y --skip-broken jakarta-commons-daemon
yum install -y --skip-broken jakarta-commons-daemon-jsvc

# install cloudstack rpm
/bin/rpm -Uvh /home/%(user)s/bcf/cloudstack-common-4.5.0-SNAPSHOT.el6.x86_64.rpm
/bin/rpm -Uvh /home/%(user)s/bcf/cloudstack-agent-4.5.0-SNAPSHOT.el6.x86_64.rpm

# use pxe gw as default gw
sed -i '/route/d' /etc/rc.d/rc.local
sed -i '/sleep/d' /etc/rc.d/rc.local
sed -i '/while/d' /etc/rc.d/rc.local
sed -i '/done/d' /etc/rc.d/rc.local
sed -i '/exit/d' /etc/rc.d/rc.local
sed -i '/lldp/d' /etc/rc.d/rc.local
echo -e "sleep 60" >> /etc/rc.d/rc.local
echo "/etc/init.d/lldpd stop >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/etc/init.d/lldpd start >> /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo -e "route del default\nwhile [[ \$? == 0 ]]; do\n    route del default\ndone" >> /etc/rc.d/rc.local
echo "route add default gw %(pxe_gw)s" >> /etc/rc.d/rc.local
sed -i '/libvirtd/d' /etc/rc.d/rc.local
echo "/sbin/service libvirtd stop /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "/sbin/service libvirtd start /home/%(user)s/bcf/%(role)s.log 2>&1" >> /etc/rc.d/rc.local
echo "exit 0" >> /etc/rc.d/rc.local
chmod 777 /etc/rc.local

reboot
'''

CENTOS_LOCAL=r'''
#!/bin/bash
ifcfgs=%(ifcfgs)s
echo -e "Start to deploy %(role)s node %(hostname)s...\n"
sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S mkdir -m 0777 -p /home/%(user)s/bcf"
ifcfg_count=${#ifcfgs[@]}
for (( i=0; i<${ifcfg_count}; i++ )); do
    ifcfg=${ifcfgs[$i]}
    echo -e "Copy /etc/sysconfig/network-scripts/${ifcfg} to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(hostname)s.${ifcfg} %(user)s@%(hostname)s:/etc/sysconfig/network-scripts/${ifcfg} >> %(log)s 2>&1
done
echo -e "Copy bonding.conf to node %(hostname)s\n"
sshpass -p %(pwd)s scp /tmp/%(hostname)s.alias %(user)s@%(hostname)s:/etc/modprobe.d/bonding.conf >> %(log)s 2>&1
echo -e "Copy %(CS_COMMON_RPM)s to node %(hostname)s\n"
sshpass -p %(pwd)s scp /tmp/%(CS_COMMON_RPM)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_COMMON_RPM)s >> %(log)s 2>&1
if [[ ! -f /tmp/vhd-util ]]; then
    wget http://download.cloud.com.s3.amazonaws.com/tools/vhd-util -P /tmp/
fi
echo -e "Copy vhd-util to node %(hostname)s\n"
sshpass -p %(pwd)s scp /tmp/vhd-util %(user)s@%(hostname)s:/home/%(user)s/bcf/ >> %(log)s 2>&1
if [[ "%(role)s" == "management" ]]; then
    echo -e "Copy %(CS_MGMT_RPM)s to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(CS_MGMT_RPM)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_MGMT_RPM)s >> %(log)s 2>&1
    echo -e "Copy %(CS_AWSAPI_RPM)s to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(CS_AWSAPI_RPM)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_AWSAPI_RPM)s >> %(log)s 2>&1
fi
if [[ "%(role)s" == "compute" ]]; then
    echo -e "Copy %(CS_AGENT_RPM)s to node %(hostname)s\n"
    sshpass -p %(pwd)s scp /tmp/%(CS_AGENT_RPM)s %(user)s@%(hostname)s:/home/%(user)s/bcf/%(CS_AGENT_RPM)s >> %(log)s 2>&1
fi
echo -e "Copy %(role)s.sh to node %(hostname)s\n"
sshpass -p %(pwd)s scp /tmp/%(hostname)s.remote.sh %(user)s@%(hostname)s:/home/%(user)s/bcf/%(role)s.sh >> %(log)s 2>&1
echo -e "Run %(role)s.sh on node %(hostname)s\n"
echo -e "Open another command prompt and use \"tail -f %(log)s\" to display the progress\n"
sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/%(role)s.sh"
echo -e "Finish deploying %(role)s on %(hostname)s\n"
'''

def get_raw_value(dic, key):
    value = dic[key]
    if type(value) in (tuple, list):
        value = value[0]
    return value

class Node(object):
    def __init__(self, node_config):
        self.hostname        = get_raw_value(node_config, 'hostname')
        self.host_name_label = get_raw_value(node_config, 'host_name_label')
        self.pxe_gw          = get_raw_value(node_config, 'pxe_gw')
        self.node_username   = get_raw_value(node_config, 'node_username')
        self.node_password   = get_raw_value(node_config, 'node_password')
        self.role            = get_raw_value(node_config, 'role')
        self.mysql_root_pwd  = get_raw_value(node_config, 'mysql_root_pwd')
        self.cloud_db_pwd    = get_raw_value(node_config, 'cloud_db_pwd')

        self.bond_name       = get_raw_value(node_config['bond_interface'], 'name')
        self.bond_interfaces = node_config['bond_interface']['interfaces']
        self.pxe_interface   = node_config['pxe_interface']

        if COMPUTE_OS == 'xenserver':
           self.xenserver_pool  = get_raw_value(node_config, 'xenserver_pool')
        else:
           self.xenserver_pool  = None

        if self.role == ROLE_MGMT:
            self.management_bond = get_raw_value(node_config, 'management_bond')
            self.bridges = None
        else:
            self.bridges = node_config['bridges']

def generate_interface_config(node):
    config =  ('auto lo\n'
               '  iface lo inet loopback\n\n')

    pxe_intf = get_raw_value(node.pxe_interface, 'interface')
    pxe_inet = get_raw_value(node.pxe_interface, 'inet')
    if pxe_inet != 'static':
        config += ('auto %(pxe_intf)s\n'
                   '  iface %(pxe_intf)s inet %(inet)s\n'
                   '  up route add default gw %(pxe_gw)s\n\n' %
                  {'pxe_intf' : pxe_intf,
                   'pxe_gw'   : node.pxe_gw,
                   'inet'     : pxe_inet})
    elif pxe_inet == 'static':
        address = get_raw_value(node.pxe_interface, 'address')
        netmask = get_raw_value(node.pxe_interface, 'netmask')
        dns = get_raw_value(node.pxe_interface, 'dns-nameservers')
        config += ('auto %(pxe_intf)s\n'
                   '  iface %(pxe_intf)s inet %(inet)s\n'
                   '  address %(address)s\n'
                   '  netmask %(netmask)s\n'
                   '  dns-nameservers %(dns)s\n'
                   '  up route add default gw %(pxe_gw)s\n\n' %
                  {'pxe_intf' : pxe_intf,
                   'pxe_gw'   : node.pxe_gw,
                   'inet'     : pxe_inet,
                   'address'  : address,
                   'netmask'  : netmask,
                   'dns'      : dns})
        

    for intf in node.bond_interfaces:
        config += ('auto %(intf)s\n'
                   '  iface %(intf)s inet manual\n'
                   '  bond-master %(bond)s\n\n' %
                  {'intf' : intf, 'bond' : node.bond_name})

    if node.role == ROLE_MGMT:
         mgmt_bond = node.management_bond
         vlan = get_raw_value(mgmt_bond, 'vlan')
         inet = get_raw_value(mgmt_bond, 'inet')

         if vlan:
             config += ('auto %(bond)s\n'
                        '  iface %(bond)s inet manual\n'
                        '  bond-mode 0\n'
                        '  bond-slaves none\n'
                        '  bond-updelay 15000\n'
                        '  bond-miimon 50\n\n' %
                       {'bond' : node.bond_name})

         address = None
         netmask = None
         if inet == 'static':
             address = get_raw_value(mgmt_bond, 'address')
             netmask = get_raw_value(mgmt_bond, 'netmask')

         if vlan and (inet != 'static'):
             config += ('auto %(bond)s.%(vlan)s\n'
                        '  iface %(bond)s.%(vlan)s inet %(inet)s\n'
                        '  vlan-raw-device %(bond)s\n\n' %
                       {'vlan' : vlan,
                        'bond' : node.bond_name,
                        'inet' : inet})
         elif vlan and (inet == 'static'):
             config += ('auto %(bond)s.%(vlan)s\n'
                        '  iface %(bond)s.%(vlan)s inet %(inet)s\n'
                        '  vlan-raw-device %(bond)s\n'
                        '  address %(address)s\n'
                        '  netmask %(netmask)s\n\n' %
                       {'vlan'    : vlan,
                        'bond'    : node.bond_name,
                        'inet'    : inet,
                        'address' : address,
                        'netmask' : netmask})
         elif (not vlan) and (inet != 'static'):
             config += ('auto %(bond)s\n'
                        '  iface %(bond)s inet %(inet)s\n'
                        '  bond-mode 0\n'
                        '  bond-slaves none\n'
                        '  bond-updelay 15000\n'
                        '  bond-miimon 50\n\n' %
                       {'bond' : node.bond_name,
                        'inet' : inet})
         elif (not vlan) and (inet == 'static'):
             config += ('auto %(bond)s\n'
                        '  iface %(bond)s inet %(inet)s\n'
                        '  address %(address)s\n'
                        '  netmask %(netmask)s\n'
                        '  bond-mode 0\n'
                        '  bond-slaves none\n'
                        '  bond-updelay 15000\n'
                        '  bond-miimon 50\n\n' %
                       {'bond'           : node.bond_name,
                        'inet'           : inet,
                        'address'        : address,
                        'netmask'        : netmask})
    else:
        config += ('auto %(bond)s\n'
               '  iface %(bond)s inet manual\n'
               '  bond-mode 0\n'
               '  bond-slaves none\n'
               '  bond-updelay 15000\n'
               '  bond-miimon 50\n\n' %
              {'bond' : node.bond_name})

        for bridge in node.bridges:
            name = get_raw_value(bridge, 'name')
            vlan = get_raw_value(bridge, 'vlan')
            inet = get_raw_value(bridge, 'inet')
            address = ""
            if 'address' in bridge.keys():
                address = get_raw_value(bridge, 'address')
            netmask = ""
            if 'netmask' in bridge.keys():
                netmask = get_raw_value(bridge, 'netmask')
            gateway = ""
            if 'gateway' in bridge.keys():
                gateway = get_raw_value(bridge, 'gateway')

            port_name = node.bond_name
            if vlan:
                port_name = ('%(bond)s.%(vlan)s' % 
                            {'vlan' : vlan,
                             'bond' : node.bond_name})
                config += ('auto %(port_name)s\n'
                           '  iface %(port_name)s inet manual\n'
                           '  vlan-raw-device %(bond)s\n\n' %
                          {'port_name' : port_name,
                           'bond'      : node.bond_name})
 
            if node.role == ROLE_COMPUTE and inet != 'static':
                config += ('auto %(name)s\n'
                           '  iface %(name)s inet %(inet)s\n'
                           '  bridge_ports %(port_name)s\n'
                           '  bridge_stp off\n'
                           '  up /sbin/ifconfig $IFACE up || /bin/true\n\n' %
                          {'name'      : name,
                           'port_name' : port_name,
                           'inet'      : inet})
            elif node.role == ROLE_COMPUTE and inet == 'static':
                config += ('auto %(name)s\n'
                           '  iface %(name)s inet %(inet)s\n'
                           '  address %(address)s\n'
                           '  netmask %(netmask)s\n'
                           '  gateway %(gateway)s\n'
                           '  bridge_ports %(port_name)s\n'
                           '  bridge_stp off\n'
                           '  up /sbin/ifconfig $IFACE up || /bin/true\n\n' %
                          {'name'      : name,
                           'port_name' : port_name,
                           'inet'      : inet,
                           'address'   : address,
                           'netmask'   : netmask,
                           'gateway'   : gateway})

    with open('/tmp/%s.intf' % node.hostname, "w") as config_file:
        config_file.write(config)
        config_file.close()


# print in python is not thread safe
print_lock = Lock()
def safe_print(message):
    with print_lock:
        run_command_on_local('stty sane')
        sys.stdout.write(message)
        sys.stdout.flush()
        run_command_on_local('stty sane')

def read_output(pipe, func):
    for lines in iter(pipe.readline, ''):
        for line in lines.splitlines(True):
            l = ''.join(filter(lambda x: 32 <= ord(x) <= 126, line.strip()))
            if len(l):
                func(l + '\n')
    pipe.close()

# function to kill expired bash script
def kill_on_timeout(command, event, timeout, proc):
    if not event.wait(timeout):
        safe_print('Timeout when running %s' % command)
        proc.kill()


# queue to store all nodes, for step 1: setup master, on master, compute.sh
node_q = Queue.Queue()
# queue to store all xen slave nodes, for step 2: join cluster, on slave, slave.sh
xen_slave_node_q = Queue.Queue()
# queue to store all xen master nodes, for step 3: assign ip, on master, bondip.sh
xen_master_node_q = Queue.Queue()
# queue to store all nodes, for step 4: change mgmt intf, on all, mgmtintf.sh
node_mgmtintf_q = Queue.Queue()
# queue to store all xen master nodes, for step 5: reboot master, on master, reboot
xen_master_node_reboot_q = Queue.Queue()
# queue to store all xen slave nodes, for step 6: reboot slave, on slave, reboot
xen_slave_node_reboot_q = Queue.Queue()
# queue to store all xen nodes, for step 7: check bond
xen_check_bond_q = Queue.Queue()


def run_command_on_local(command, timeout=1800):
    event = threading.Event()
    p = subprocess.Popen(
        command, shell=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, bufsize=1)

    tout = threading.Thread(
        target=read_output, args=(p.stdout, safe_print))
    terr = threading.Thread(
        target=read_output, args=(p.stderr, safe_print))
    for t in (tout, terr):
        t.daemon = True
        t.start()

    watcher = threading.Thread(
        target=kill_on_timeout, args=(command, event, timeout, p))
    watcher.daemon = True
    watcher.start()

    p.wait()
    event.set()
    for t in (tout, terr):
        t.join()


def generate_command_for_node(node):
    if COMPUTE_OS == "ubuntu" or (node.role == "management" and MGMT_OS == 'ubuntu'):
        # generate interface config
        generate_interface_config(node)

        # generate puppet script
        intfs = ','.join(node.bond_interfaces)
        lldp_config = LLDP_PUPPET % {'bond_interfaces' : intfs}
        node_config = None
        if node.role == ROLE_MGMT:
            node_config = (MGMT_PUPPET %
                          {'user'                : node.node_username,
                           'role'                : node.role,
                           'mysql_root_pwd'      : node.mysql_root_pwd,
                           'cs_url'              : CS_URL,
                           'cs_common'           : CS_COMMON,
                           'cs_mgmt'             : CS_MGMT,
                           'cloud_db_pwd'        : node.cloud_db_pwd,
                           'storage_script'      : STORAGE_SCRIPT,
                           'storage_vm_url'      : STORAGE_VM_URL,
                           'storage_vm_template' : STORAGE_VM_TEMPLATE})
        elif node.role == ROLE_COMPUTE:
            node_config = (COMPUTE_PUPPET %
                          {'user'      : node.node_username,
                           'role'      : node.role,
                           'cs_url'    : CS_URL,
                           'cs_common' : CS_COMMON,
                           'cs_agent'  : CS_AGENT,
                           'pxe_gw'    : node.pxe_gw})
        with open('/tmp/%s.pp' % node.hostname, "w") as node_puppet:
            node_puppet.write("%(node_config)s\n\n%(lldp_config)s" %
                             {'node_config' : node_config,
                              'lldp_config' : lldp_config})
            node_puppet.close()

        # generate db shell script
        if node.role == ROLE_MGMT:
            with open('/tmp/%s.db.sh' % node.hostname, "w") as node_db_bash:
                node_db_bash.write(DB_BASH %
                                  {'user'           : node.node_username,
                                   'role'           : node.role,
                                   'cloud_db_pwd'   : node.cloud_db_pwd,
                                   'mysql_root_pwd' : node.mysql_root_pwd,
                                   'hostname'       : node.hostname})
                node_db_bash.close()

        # generate remote shell script
        bond_intfs = '('
        for bond_interface in node.bond_interfaces:
            bond_intfs += r'''"%s" ''' % bond_interface
        bond_intfs += ')'

        network_name_labels = '('
        vlan_tags  = '('
        bond_inets = '('
        bond_ips   = '('
        bond_masks = '('
        bond_gateways = '('
        if node.bridges:
            for bridge in node.bridges:
                name = get_raw_value(bridge, 'name')
                vlan = get_raw_value(bridge, 'vlan')
                if not vlan:
                    vlan = ""
                inet = get_raw_value(bridge, 'inet')
                address = ""
                if 'address' in bridge.keys():
                    address = get_raw_value(bridge, 'address')
                netmask = ""
                if 'netmask' in bridge.keys():
                    netmask = get_raw_value(bridge, 'netmask')
                gateway = ""
                if 'gateway' in bridge.keys():
                    gateway = get_raw_value(bridge, 'gateway')
                network_name_labels += r'''"%s" ''' % name
                vlan_tags  += r'''"%s" ''' % vlan
                bond_inets += r'''"%s" ''' % inet
                bond_ips   += r'''"%s" ''' % address
                bond_masks += r'''"%s" ''' % netmask
                bond_gateways += r'''"%s" ''' % gateway
        network_name_labels += ')'
        vlan_tags  += ')'
        bond_inets += ')'
        bond_ips   += ')'
        bond_masks += ')'
        bond_gateways += ')'

        pxe_intf = get_raw_value(node.pxe_interface, 'interface')
        pxe_inet = get_raw_value(node.pxe_interface, 'inet')
        pxe_address = ""
        pxe_netmask = ""
        pxe_dns     = ""
        if pxe_inet == 'static':
            pxe_address = get_raw_value(node.pxe_interface, 'address')
            pxe_netmask = get_raw_value(node.pxe_interface, 'netmask')
            pxe_dns     = get_raw_value(node.pxe_interface, 'dns-nameservers')

        with open('/tmp/%s.remote.sh' % node.hostname, "w") as node_remote_bash:
            node_remote_bash.write(NODE_REMOTE_BASH %
                                  {'user'                : node.node_username,
                                   'role'                : node.role,
                                   'cloud_db_pwd'        : node.cloud_db_pwd,
                                   'mysql_root_pwd'      : node.mysql_root_pwd,
                                   'hostname'            : node.hostname,
                                   'COMPUTE_OS'          : COMPUTE_OS,
                                   'host_name_label'     : node.host_name_label,
                                   'network_name_labels' : network_name_labels,
                                   'vlan_tags'           : vlan_tags,
                                   'bond_intfs'          : bond_intfs,
                                   'bond_inets'          : bond_inets,
                                   'bond_ips'            : bond_ips,
                                   'bond_masks'          : bond_masks,
                                   'bond_gateways'       : bond_gateways,
                                   'pxe_intf'            : pxe_intf,
                                   'pxe_inet'            : pxe_inet,
                                   'pxe_address'         : pxe_address,
                                   'pxe_netmask'         : pxe_netmask,
                                   'pxe_gw'              : node.pxe_gw,
                                   'pxe_dns'             : pxe_dns})
            node_remote_bash.close()

        # generate local script for node
        with open('/tmp/%s.local.sh' % node.hostname, "w") as node_local_bash:
            node_local_bash.write(NODE_LOCAL_BASH %
                                 {'pwd'        : node.node_password,
                                  'hostname'   : node.hostname,
                                  'user'       : node.node_username,
                                  'role'       : node.role,
                                  'pool'       : node.xenserver_pool,
                                  'log'        : LOG_FILENAME,
                                  'COMPUTE_OS' : COMPUTE_OS,
                                  'CS_COMMON'  : CS_COMMON,
                                  'CS_MGMT'    : CS_MGMT,
                                  'CS_AGENT'   : CS_AGENT})
            node_local_bash.close()

    if node.role == "management" and MGMT_OS == 'centos':
        # generate interface configuration
        intf_files = '('
        for intf in node.bond_interfaces:
            intf_files += r'''"ifcfg-%s" ''' % intf
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : intf}), "w") as intf_conf:
                intf_conf.write(CENTOS_ETH %
                               {'device'    : intf,
                                'bond_name' : node.bond_name})
                intf_conf.close()
        mgmt_bond = node.management_bond
        vlan = get_raw_value(mgmt_bond, 'vlan')
        inet = get_raw_value(mgmt_bond, 'inet')

        address = None
        network = None
        netmask = None
        if inet == 'static':
            address = get_raw_value(mgmt_bond, 'address')
            network = get_raw_value(mgmt_bond, 'network')
            netmask = get_raw_value(mgmt_bond, 'netmask')

        intf_files += r'''"ifcfg-%s" ''' % node.bond_name
        if vlan and (inet == 'dhcp'):
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : node.bond_name}), "w") as base_bond:
                base_bond.write(CENTOS_BASE_BOND %
                               {'bond_name' : node.bond_name})
                base_bond.close()
            intf_files += (r'''"ifcfg-%(bond_name)s.%(vlan)d" ''' %
                          {'bond_name' : node.bond_name,
                            'vlan'     : vlan})
            with open(("/tmp/%(hostname)s.ifcfg-%(node_bond)s.%(vlan)d" %
                      {'hostname'  : node.hostname,
                       'node_bond' : node.bond_name,
                       'vlan'      : vlan}), "w") as tagged_dhcp_bond:
                tagged_dhcp_bond.write(CENTOS_TAGGED_DHCP_BOND %
                                      {'bond_name' : node.bond_name,
                                       'vlan'      : vlan,})
                tagged_dhcp_bond.close()
        elif vlan and (inet == 'static'):
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : node.bond_name}), "w") as base_bond:
                base_bond.write(CENTOS_BASE_BOND %
                               {'bond_name' : node.bond_name})
                base_bond.close()
            intf_files += (r'''"ifcfg-%(bond_name)s.%(vlan)d" ''' %
                          {'bond_name' : node.bond_name,
                            'vlan'     : vlan})
            with open(("/tmp/%(hostname)s.ifcfg-%(node_bond)s.%(vlan)d" %
                      {'hostname'  : node.hostname,
                       'node_bond' : node.bond_name,
                       'vlan'      : vlan}), "w") as tagged_static_bond:
                tagged_static_bond.write(CENTOS_TAGGED_STATIC_BOND %
                                        {'bond_name' : node.bond_name,
                                         'vlan'      : vlan,
                                         'address'   : address,
                                         'network'   : network,
                                         'netmask'   : netmask})
                tagged_static_bond.close()
        elif (not vlan) and (inet == 'dhcp'):
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : node.bond_name}), "w") as base_dhcp_bond:
                base_dhcp_bond.write(CENTOS_BASE_DHCP_BOND %
                                    {'bond_name' : node.bond_name})
                base_dhcp_bond.close()
        elif (not vlan) and (inet == 'static'):
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : node.bond_name}), "w") as base_static_bond:
                base_static_bond.write(CENTOS_BASE_STATIC_BOND %
                                      {'bond_name' : node.bond_name,
                                       'address'   : address,
                                       'network'   : network,
                                       'netmask'   : netmask})
                base_static_bond.close()

        with open("/tmp/%(hostname)s.alias" % {'hostname' : node.hostname}, "w") as bond_alias:
            bond_alias.write(CENTOS_BOND_ALIAS %
                            {'bond_name' : node.bond_name})
            bond_alias.close()
        intf_files += ')'

        # generate remote script
        with open("/tmp/%(hostname)s.remote.sh" % {'hostname' : node.hostname}, "w") as centos_remote:
            intfs = ','.join(node.bond_interfaces)
            centos_remote.write(CENTOS_MGMT_REMOTE %
                               {'bond_intfs'     : intfs,
                                'user'           : node.node_username,
                                'mysql_root_pwd' : node.mysql_root_pwd,
                                'cloud_db_pwd'   : node.cloud_db_pwd,
                                'hostname'       : node.hostname,
                                'role'           : node.role,
                                'pxe_gw'         : node.pxe_gw})
            centos_remote.close()

        # generate local script
        with open("/tmp/%(hostname)s.local.sh" % {'hostname' : node.hostname}, "w") as centos_local:
            centos_local.write(CENTOS_LOCAL %
                              {'ifcfgs'        : intf_files,
                               'role'          : node.role,
                               'hostname'      : node.hostname,
                               'pwd'           : node.node_password,
                               'user'          : node.node_username,
                               'log'           : LOG_FILENAME,
                               'CS_COMMON_RPM' : CS_COMMON_RPM,
                               'CS_MGMT_RPM'   : CS_MGMT_RPM,
                               'CS_AWSAPI_RPM' : CS_AWSAPI_RPM,
                               'CS_AGENT_RPM'  : CS_AGENT_RPM})
            centos_local.close()

    if node.role == "compute" and COMPUTE_OS == 'centos':
        intf_files = '('
        for intf in node.bond_interfaces:
            intf_files += r'''"ifcfg-%s" ''' % intf
            with open(("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                      {'hostname' : node.hostname,
                       'intf'     : intf}), "w") as intf_conf:
                intf_conf.write(CENTOS_ETH %
                               {'device'    : intf,
                                'bond_name' : node.bond_name})
                intf_conf.close()

        if node.bridges:
            for bridge in node.bridges:
                name = get_raw_value(bridge, 'name')
                vlan = get_raw_value(bridge, 'vlan')
                inet = get_raw_value(bridge, 'inet')
                address = ""
                if 'address' in bridge.keys():
                    address = get_raw_value(bridge, 'address')
                network = ""
                if 'network' in bridge.keys():
                    network = get_raw_value(bridge, 'network')
                netmask = ""
                if 'netmask' in bridge.keys():
                    netmask = get_raw_value(bridge, 'netmask')
                gateway = ""
                if 'gateway' in bridge.keys():
                    gateway = get_raw_value(bridge, 'gateway')

                intf_files += r'''"ifcfg-%s" ''' % name
                with open("/tmp/%(hostname)s.ifcfg-%(bridge)s" %
                         {'hostname' : node.hostname,
                          'bridge'   : name}, "w") as bridge_file:
                    if inet == 'static':
                        bridge_file.write(CENTOS_STATIC_BRIDGE %
                                         {'bridge_name' : name,
                                          'address'     : address,
                                          'network'     : network,
                                          'netmask'     : netmask,
                                          'gateway'     : gateway})
                    else:
                       bridge_file.write(CENTOS_DHCP_BRIDGE %
                                        {'bridge_name' : name})
                    bridge_file.close()
                if not vlan:
                    intf_files += r'''"ifcfg-%s" ''' % node.bond_name
                    with open("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                             {'hostname' : node.hostname,
                              'intf'     : node.bond_name}, "w") as base_bridge_bond:
                        base_bridge_bond.write(CENTOS_BASE_BRIDGE_BOND %
                                              {'bond_name'   : node.bond_name,
                                               'bridge_name' : name})
                        base_bridge_bond.close()
                else:
                    intf_files += (r'''"ifcfg-%(intf)s.%(vlan)s" ''' %
                                  {'intf' : node.bond_name,
                                   'vlan' : vlan})
                    with open("/tmp/%(hostname)s.ifcfg-%(intf)s.%(vlan)s" %
                             {'hostname' : node.hostname,
                              'intf'     : node.bond_name,
                              'vlan'     : vlan}, "w") as base_bridge_bond:
                        base_bridge_bond.write(CENTOS_TAGGED_BRIDGE_BOND %
                                              {'bond_name'   : node.bond_name,
                                               'vlan'        : vlan,
                                               'bridge_name' : name})
                        base_bridge_bond.close()
            if not os.path.isfile("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                                 {'hostname' : node.hostname,
                                  'intf'     : node.bond_name}):
                intf_files += r'''"ifcfg-%s" ''' % node.bond_name
                with open("/tmp/%(hostname)s.ifcfg-%(intf)s" %
                         {'hostname' : node.hostname,
                          'intf'     : node.bond_name}, "w") as base_bond:
                    base_bond.write(CENTOS_BASE_BOND %
                                   {'bond_name'   : node.bond_name})
                    base_bond.close()

        intf_files += ')'

        # bond alias
        with open("/tmp/%(hostname)s.alias" % {'hostname' : node.hostname}, "w") as bond_alias:
            bond_alias.write(CENTOS_BOND_ALIAS %
                            {'bond_name' : node.bond_name})
            bond_alias.close()

        # generate remote script
        with open("/tmp/%(hostname)s.remote.sh" % {'hostname' : node.hostname}, "w") as centos_remote:
            intfs = ','.join(node.bond_interfaces)
            centos_remote.write(CENTOS_COMPUTE_REMOTE %
                               {'bond_intfs'     : intfs,
                                'user'           : node.node_username,
                                'role'           : node.role,
                                'pxe_gw'         : node.pxe_gw})
            centos_remote.close()

        # generate local script
        with open("/tmp/%(hostname)s.local.sh" % {'hostname' : node.hostname}, "w") as centos_local:
            centos_local.write(CENTOS_LOCAL %
                              {'ifcfgs'        : intf_files,
                               'role'          : node.role,
                               'hostname'      : node.hostname,
                               'pwd'           : node.node_password,
                               'user'          : node.node_username,
                               'log'           : LOG_FILENAME,
                               'CS_COMMON_RPM' : CS_COMMON_RPM,
                               'CS_MGMT_RPM'   : CS_MGMT_RPM,
                               'CS_AWSAPI_RPM' : CS_AWSAPI_RPM,
                               'CS_AGENT_RPM'  : CS_AGENT_RPM})
            centos_local.close()

    if COMPUTE_OS == "xenserver" and node.role == "compute":
        # generate script for xen slaves
        if MASTER_NODES[node.xenserver_pool].hostname != node.hostname:
            with open('/tmp/%s.slave.sh' % node.hostname, "w") as slave_bash:
                slave_bash.write(XEN_SLAVE %
                                {'master_address'  : MASTER_NODES[node.xenserver_pool].hostname,
                                 'master_username' : MASTER_NODES[node.xenserver_pool].node_username,
                                 'master_pwd'      : MASTER_NODES[node.xenserver_pool].node_password,
                                 'bond_intfs'      : bond_intfs,
                                 'username'        : node.node_username,
                                 'pxe_gw'          : node.pxe_gw,
                                 'host_name_label' : node.host_name_label})
                slave_bash.close()
            with open('/tmp/%s.slave_reboot.sh' % node.hostname, "w") as slave_reboot_bash:
                slave_reboot_bash.write(XEN_SLAVE_REBOOT %
                                {'master_address' : MASTER_NODES[node.xenserver_pool].hostname})
                slave_reboot_bash.close()

        with open('/tmp/%s.checkbond.sh' % node.hostname, "w") as checkbond_bash:
            checkbond_bash.write(XEN_CHECK_BOND %
                               {'hostname'   : node.hostname,
                                'pwd'        : node.node_password,
                                'user'       : node.node_username,
                                'intf_count' : len(node.bond_interfaces),
                                'log'        : LOG_FILENAME})
            checkbond_bash.close()

        with open('/tmp/%s.mgmtintf.sh' % node.hostname, "w") as mgmtintf_bash:
            mgmtintf_bash.write(XEN_CHANGE_MGMT_INTF %
                               {'host_name_label'  : node.host_name_label})
            mgmtintf_bash.close()

# step 0: setup management node
def worker_setup_management():
    cmd = 'bash /tmp/%s.local.sh' % MANAGEMENT_NODE.hostname
    run_command_on_local(cmd)

# step 1: setup master, on master, compute.sh
def worker_setup_master():
    while True:
        node = node_q.get()
        cmd = 'bash /tmp/%s.local.sh' % node.hostname
        run_command_on_local(cmd)
        node_q.task_done()

# step 2: join cluster, on slave, slave.s
def worker_join_cluster():
    while True:
        node = xen_slave_node_q.get()
        cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/slave.sh"''' %
               {'pwd'      : node.node_password,
                'user'     : node.node_username,
                'hostname' : node.hostname,
                'log'      : LOG_FILENAME})
        run_command_on_local(cmd)
        xen_slave_node_q.task_done()

# step 3: assign ip, on master, bondip.sh
def worker_assign_ip():
    while True:
        node = xen_master_node_q.get()
        cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/bondip.sh"''' %
               {'pwd'      : node.node_password,
                'user'     : node.node_username,
                'hostname' : node.hostname,
                'log'      : LOG_FILENAME})
        run_command_on_local(cmd)
        xen_master_node_q.task_done()

# step 4: change mgmt intf, on all, mgmtintf.sh
def worker_change_mgmtintf():
    while True:
        node = node_mgmtintf_q.get()
        cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/mgmtintf.sh"''' %
               {'pwd'      : node.node_password,
                'user'     : node.node_username,
                'hostname' : node.hostname,
                'log'      : LOG_FILENAME})
        run_command_on_local(cmd)
        node_mgmtintf_q.task_done()

# step 5: reboot master, on master, reboot
def worker_reboot_master():
    while True:
        node = xen_master_node_reboot_q.get()
        cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S reboot"''' %
               {'pwd'      : node.node_password,
                'user'     : node.node_username,
                'hostname' : node.hostname,
                'log'      : LOG_FILENAME})
        run_command_on_local(cmd)
        xen_master_node_reboot_q.task_done()

# step 6: reboot slave, on master, reboot
def worker_reboot_slave():
    while True:
        node = xen_slave_node_reboot_q.get()
        cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S bash /home/%(user)s/bcf/slave_reboot.sh"''' %
               {'pwd'      : node.node_password,
                'user'     : node.node_username,
                'hostname' : node.hostname,
                'log'      : LOG_FILENAME})
        run_command_on_local(cmd)
        xen_slave_node_reboot_q.task_done()

# step 7: reboot management
def worker_reboot_management():
    cmd = (r'''sshpass -p %(pwd)s ssh -t -oStrictHostKeyChecking=no -o LogLevel=quiet %(user)s@%(hostname)s >> %(log)s 2>&1 "echo %(pwd)s | sudo -S reboot"''' %
           {'pwd'      : MANAGEMENT_NODE.node_password,
            'user'     : MANAGEMENT_NODE.node_username,
            'hostname' : MANAGEMENT_NODE.hostname,
            'log'      : LOG_FILENAME})
    run_command_on_local(cmd)

# step 7: check bond of all xen server
def worker_check_bond():
    while True:
        node = xen_check_bond_q.get()
        safe_print("start to check bond on %s\n" % node.hostname)
        cmd = (r'''bash /tmp/%s.checkbond.sh''' % node.hostname)
        run_command_on_local(cmd)
        xen_check_bond_q.task_done()
        safe_print("finish checking bond on %s\n" % node.hostname)

def deploy_to_all(config):
    # install sshpass
    safe_print("Prepare cloud stack packages\n")
    run_command_on_local(
        'sudo mkdir -p /tmp;'
        'sudo rm -f /tmp/*.deb;'
        'sudo rm -f /tmp/*.sh;'
        'sudo cp %(CS_COMMON)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_MGMT)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_AGENT)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_COMMON_RPM)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_MGMT_RPM)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_AGENT_RPM)s /tmp/ >> %(log)s 2>&1;'
        'sudo cp %(CS_AWSAPI_RPM)s /tmp/ >> %(log)s 2>&1' %
       {'CS_COMMON'     : CS_COMMON,
        'CS_MGMT'       : CS_MGMT,
        'CS_AGENT'      : CS_AGENT,
        'CS_COMMON_RPM' : CS_COMMON_RPM,
        'CS_MGMT_RPM'   : CS_MGMT_RPM,
        'CS_AGENT_RPM'  : CS_AGENT_RPM,
        'CS_AWSAPI_RPM' : CS_AWSAPI_RPM,
        'log'           : LOG_FILENAME})

    global COMPUTE_OS
    global MGMT_OS
    global COMPUTE_OS
    global MASTER_NODES
    global POOL_SIZES
    global MANAGEMENT_NODE
    COMPUTE_OS = config['compute_os']
    MGMT_OS = config['management_os']

    slave_name_labels_dic = {}
    bond_ips_dic   = {}
    bond_masks_dic = {}
    bond_gateways_dic = {}
    bond_vlans_dic = {}
    bond_inets_dic = {}

    for node_config in config['nodes']:
        if 'pxe_interface' not in node_config:
            node_config['pxe_interface'] = config['default_pxe_interface']
        if 'node_username' not in node_config:
            node_config['node_username'] = config['default_node_username']
        if 'node_password' not in node_config:
            node_config['node_password'] = config['default_node_password']
        if 'role' not in node_config:
            node_config['role'] = config['default_role']
        if COMPUTE_OS == 'xenserver' and 'xenserver_pool' not in node_config:
            node_config['xenserver_pool'] = config['default_xenserver_pool']
        if 'bond_interface' not in node_config:
            node_config['bond_interface'] = config['default_bond_interface']
        if 'bridges' not in node_config:
            node_config['bridges'] = config['default_bridges']
        if 'host_name_label' not in node_config:
            node_config['host_name_label'] = ''
        node_config['pxe_gw'] = config['pxe_gw']
        node_config['mysql_root_pwd'] = config['mysql_root_pwd']
        if not node_config['mysql_root_pwd']:
            node_config['mysql_root_pwd'] = UNDEF
        node_config['cloud_db_pwd'] = config['cloud_db_pwd']
        if not node_config['cloud_db_pwd']:
            node_config['cloud_db_pwd'] = UNDEF

        node = Node(node_config)
        run_command_on_local('sudo rm -rf /tmp/%(hostname)s*' % {'hostname' : node.hostname})
        if node.role == "management":
            MANAGEMENT_NODE = node
        else:
            node_q.put(node)
            node_mgmtintf_q.put(node)
            xen_check_bond_q.put(node)

        if COMPUTE_OS == "xenserver" and node.role == "compute" and node.xenserver_pool not in MASTER_NODES.keys():
            MASTER_NODES[node.xenserver_pool] = node
            POOL_SIZES[node.xenserver_pool] = 1
            slave_name_labels_dic[node.xenserver_pool] = '('
            bond_ips_dic[node.xenserver_pool] = '('
            bond_masks_dic[node.xenserver_pool] = '('
            bond_gateways_dic[node.xenserver_pool] = '('
            bond_vlans_dic[node.xenserver_pool] = '('
            bond_inets_dic[node.xenserver_pool] = '('
            if node.bridges:
                for bridge in node.bridges:
                    vlan = get_raw_value(bridge, 'vlan')
                    if not vlan:
                        vlan = ""
                    inet = get_raw_value(bridge, 'inet')
                    bond_vlans_dic[node.xenserver_pool] += r'''"%s" ''' % vlan
                    bond_inets_dic[node.xenserver_pool] += r'''"%s" ''' % inet
                bond_vlans_dic[node.xenserver_pool] += ')'
                bond_inets_dic[node.xenserver_pool] += ')'
            xen_master_node_q.put(node)
            xen_master_node_reboot_q.put(node)
            safe_print("Master node of xenserver pool %(pool)s is: %(hostname)s\n" %
                       {'pool'     : node.xenserver_pool,
                        'hostname' : node.hostname})
        elif COMPUTE_OS == "xenserver" and node.role == "compute":
            POOL_SIZES[node.xenserver_pool] = POOL_SIZES.get(node.xenserver_pool, 1) + 1
            slave_name_labels_dic[node.xenserver_pool] += r'''"%s" ''' % node.host_name_label
            if node.bridges:
                for bridge in node.bridges:
                    address = ""
                    if 'address' in bridge.keys():
                        address = get_raw_value(bridge, 'address')
                    netmask = ""
                    if 'netmask' in bridge.keys():
                        netmask = get_raw_value(bridge, 'netmask')
                    gateway = ""
                    if 'gateway' in bridge.keys():
                        gateway = get_raw_value(bridge, 'gateway')
                    bond_ips_dic[node.xenserver_pool] += r'''"%s" ''' % address
                    bond_masks_dic[node.xenserver_pool] += r'''"%s" ''' % netmask
                    bond_gateways_dic[node.xenserver_pool] += r'''"%s" ''' % gateway
            xen_slave_node_q.put(node)
            xen_slave_node_reboot_q.put(node)

        generate_command_for_node(node)

    for pool in MASTER_NODES.keys():
        slave_name_labels_dic[pool] += ')'
        bond_ips_dic[pool] += ')'
        bond_masks_dic[pool] += ')'
        bond_gateways_dic[pool] += ')'
        # generate ip assignment script for xen master node
        with open('/tmp/%(hostname)s.%(pool)s.bondip.sh' %
                 {'hostname' : MASTER_NODES[pool].hostname,
                  'pool'     : pool}, "w") as bondip_bash:
            bondip_bash.write(XEN_IP_ASSIGNMENT %
                             {'username'          : MASTER_NODES[pool].node_username,
                              'cluster_size'      : POOL_SIZES[pool],
                              'slave_name_labels' : slave_name_labels_dic[pool],
                              'bond_vlans'        : bond_vlans_dic[pool],
                              'bond_inets'        : bond_inets_dic[pool],
                              'bond_ips'          : bond_ips_dic[pool],
                              'bond_masks'        : bond_masks_dic[pool],
                              'bond_gateways'     : bond_gateways_dic[pool],
                              'xenserver_pool'    : MASTER_NODES[pool].xenserver_pool})
            bondip_bash.close()

    if (MANAGEMENT_NODE or COMPUTE_OS != 'xenserver') and (not os.path.isfile("/tmp/%s" % CS_COMMON_RPM)) and (not os.path.isfile("/tmp/%s" % CS_COMMON)):
       safe_print("cloudstack common package is missing\n")
       return
    if (MANAGEMENT_NODE) and (not os.path.isfile("/tmp/%s" % CS_MGMT_RPM)) and (not os.path.isfile("/tmp/%s" % CS_MGMT)):
       safe_print("cloudstack management package is missing\n")
       return
    if (COMPUTE_OS == 'ubuntu') and (node_q.qsize() > 0) and (not os.path.isfile("/tmp/%s" % CS_AGENT)):
       safe_print("cloudstack agent package is missing\n")
       return
    if (COMPUTE_OS == 'centos') and (node_q.qsize() > 0) and (not os.path.isfile("/tmp/%s" % CS_AGENT_RPM)):
       safe_print("cloudstack agent package is missing\n")
       return
    if (MGMT_OS == 'centos') and (MANAGEMENT_NODE) and (not os.path.isfile("/tmp/%s" % CS_AWSAPI_RPM)):
       safe_print("cloudstack awsapi package is missing\n")
       return

    # step 0: setup management node
    if MANAGEMENT_NODE:
        management_node_thread = threading.Thread(target=worker_setup_management)
        management_node_thread.daemon = True
        management_node_thread.start()

    # step 1: setup master, using node_q, on master run compute.sh   
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_setup_master)
        t.daemon = True
        t.start()
    node_q.join()
    if COMPUTE_OS == "ubuntu" or COMPUTE_OS == "centos":
        if MANAGEMENT_NODE:
            management_node_thread.join()
            safe_print("Finish deploying management node\n")
            safe_print("CloudStack deployment finished\n")
            t = threading.Thread(target=worker_reboot_management)
            t.daemon = True
            t.start()
            t.join()
            return
        safe_print("CloudStack deployment finished\n")
        return
    else:
        safe_print("Finish step 1: setup xen master\n")

    # step 2: join cluster, using xen_slave_node_q, on slave run slave.sh
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_join_cluster)
        t.daemon = True
        t.start()
    xen_slave_node_q.join()
    safe_print("Finish step 2: join cluster\n")

    # step 3: assign ip, using xen_master_node_q, on master run bondip.sh
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_assign_ip)
        t.daemon = True
        t.start()
    xen_master_node_q.join()
    safe_print("Finish step 3: assign ip to bond interfaces\n")

    # step 4: change mgmt intf, using node_mgmtintf_q, on all run mgmtintf.sh
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_change_mgmtintf)
        t.daemon = True
        t.start()
    node_mgmtintf_q.join()
    safe_print("Finish step 4: change management interfaces\n")

    # step 5: reboot master, using xen_master_node_reboot_q, on master using reboot
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_reboot_master)
        t.daemon = True
        t.start()
    xen_master_node_reboot_q.join()
    safe_print("Finish step 5: reboot xen masters\n")
    time.sleep(60)

    # step 6: reboot slave, using xen_slave_node_reboot_q, on slave run reboot
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_reboot_slave)
        t.daemon = True
        t.start()
    xen_slave_node_reboot_q.join()
    safe_print("Finish step 6: reboot xen slaves\n")

    # step 7: check all xen nodes' bond
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_check_bond)
        t.daemon = True
        t.start()
    xen_check_bond_q.join()
    safe_print("Finish step 7: verify bonds on all xen servers. Check %s for result.\n" % LOG_FILENAME)

    if MANAGEMENT_NODE:
        management_node_thread.join()
        safe_print("Finish deploying management node\n")
        safe_print("CloudStack deployment finished\n")
        t = threading.Thread(target=worker_reboot_management)
        t.daemon = True
        t.start()
        t.join()
        return

    safe_print("CloudStack deployment finished\n")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config-file", required=False,
                        help="CloudStack YAML config path")
    args = parser.parse_args()
    if args.config_file:
        code = subprocess.call("ping www.bigswitch.com -c1", shell=True)
        if code != 0:
            safe_print("DNS is not configured correctly, quit deployment\n")
        else:
            safe_print("Start to setup CloudStack for Big Cloud Fabric\n")
            config_file_path = args.config_file
            with open(config_file_path, 'r') as config_file:
                config = yaml.load(config_file)
            deploy_to_all(config)
    else:
        safe_print("This script supports BCF 2.5.\n"
                   "Ubuntu 12.04, centos 6.5 or centos 6.6 as the CloudStack management node.\n"
                   "CloudStack compute node can be either Ubuntu 12.04 centos 6.5, centos 6.6 or XenServer 6.2.\n"
                   "Use -h for how to use this script.\n")

