#!/bin/sh
#
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2011, Big Switch Networks, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Mandeep Dhami, Big Switch Networks, Inc.
# @author: Kevin Benton, Big Switch Networks, Inc.
#

# USAGE
# Install ivs on nova compute nodes. Use as:
#   ./install-ivs-node.sh <comma-separated-list-of-controllers>
#
# e.g.
#   ./install-ivs-node.sh 192.168.1.1,192.168.1.2
USAGE="$0 <comma-separated-list-of-controllers>"


# Globals
set -e
NETWORK_CONTROLERS=

# Process args
NETWORK_CONTROLERS=$1
echo -n "Installing OVS managed by the openflow controllers:"
echo ${NETWORK_CONTROLERS}
if [ "${NETWORK_CONTROLERS}"x = ""x ] ; then
    echo "USAGE: $USAGE" 2>&1
    echo "  >  No Network Controller specified." 1>&2
    exit 1
fi

# remove old openvswitch modules
echo "Removing old openvswitch modules..."
sudo rmmod openvswitch 2>/dev/null || :
sudo rmmod openvswitch_mod 2>/dev/null || :

# IVS INSTALL
(
  echo "ivs install"
  # download ivs
  mkdir ${HOME}/ivs || :
  cd ${HOME}/ivs
  OVSDP_PKG='https://github.com/bigswitch/deployment-support/raw/guinness/ovs/openvswitch-datapath-dkms_1.9.0-1bsn12_all.deb'
  echo "Downloading ${OVSDP_PKG} ..."
  wget "${OVSDP_PKG}"
  echo "Done ${OVSDP_PKG}/$i \n\n"
  IVS_PKG='https://github.com/bigswitch/deployment-support/raw/guinness/ivs/ivs_0.3_amd64.deb'
  echo "Downloading ${IVS_PKG} ..."
  wget "${IVS_PKG}"
  echo "Done ${IVS_PKG}/$i \n\n"

  # install openvswitch datapath
  sudo apt-get -fy install dkms libnl-route-3-200
  sudo dpkg -i openvswitch-datapath-dkms_1.9.0-1bsn12_all.deb

  # install ivs
  sudo dpkg -i ivs_0.3_amd64.deb
)

ctrls=
for ctrl in `echo ${NETWORK_CONTROLERS} | tr ',' ' '`
do
    ctrls="${ctrls} -c ${ctrl}:6633"
done
echo "Adding Network controlers: " ${ctrls}

echo "#generated by install-ivs-node.sh" | sudo tee /etc/default/ivs 1>/dev/null
echo "DAEMON_ARGS=\"${ctrls}\"" | sudo tee -a /etc/default/ivs 1>/dev/null
sudo /etc/init.d/ivs restart
echo "Add any data interfaces to the ivs switch by appending '-i <interface>' for each interface to the DAEMON_ARGS in /etc/default/ivs"

# Add init scripts
cat <<'EOF' | sudo tee /etc/init/bsn-nova.conf 1>/dev/null
#
# BSN script for nova functions to execute on reboot
#

start on started rc
task
script
  exec 1>/tmp/bsn-nova.log 2>&1
  echo `date` bsn-nova-init "Started ..."

  if [ -f /etc/bsn_tunnel_mac ] ; then
    TUN_LOOPBACK_IF=`head -1 /etc/bsn_tunnel_interface`
    TUN_LOOPBACK_MAC=`head -1 /etc/bsn_tunnel_mac`
    echo `date` bsn-nova-init "Setting ${TUN_LOOPBACK_IF} interface mac to ${TUN_LOOPBACK_MAC} ..."
    /sbin/ip link set dev "${TUN_LOOPBACK_IF}" address "${TUN_LOOPBACK_MAC}" || :
    echo `date` bsn-nova-init "Setting ${TUN_LOOPBACK_IF} interface mac to ${TUN_LOOPBACK_MAC} ... Done"
  fi

  if [ -f /etc/quantum/plugins/bigswitch/metadata_interface ] ; then
    METADATA_IF=`head -1 /etc/quantum/plugins/bigswitch/metadata_interface`
    METADATA_PORT=`head -1 /etc/quantum/plugins/bigswitch/metadata_port`
    echo `date` bsn-nova-init "Setting up metadata server address/nat on ${METADATA_IF}, port ${METADATA_PORT} ..."
    /sbin/ip addr add 169.254.169.254/32 scope link dev "${METADATA_IF}" || :
    /sbin/iptables -t nat -A PREROUTING -d 169.254.169.254/32 -p tcp -m tcp --dport 80 -j DNAT --to-destination 169.254.169.254:"${METADATA_PORT}" || :
    echo `date` bsn-nova-init "Setting up metadata server address/nat on ${METADATA_IF}, port ${METADATA_PORT} ... Done"
  fi

  if [ -f /etc/init/quantum-dhcp-agent.conf -o -f /etc/init/nova-compute.conf ] ; then
    echo `date` bsn-nova-init "Cleaning up tuntap interfaces ..."
    if [ -f /etc/init/quantum-dhcp-agent.conf ] ; then
      /usr/sbin/service quantum-dhcp-agent stop || :
    fi
    if [ -f /etc/init/nova-compute.conf ] ; then
      /usr/sbin/service nova-compute stop || :
      echo "resume_guests_state_on_host_boot=true" >> /etc/nova/nova.conf
      for qvo in `ifconfig -a | grep qvo | cut -d' ' -f1`
      do
        `sudo ovs-vsctl del-port br-int $qvo` || true
      done
      echo `date` bsn-nova-init "Cleaning up OVS ports ... Done"
      for qvb in `ifconfig -a | grep qvb | cut -d' ' -f1`
      do
        `sudo ip link set $qvb down` || true
        `sudo ip link delete $qvb` || true
      done
      echo `date` bsn-nova-init "Cleaning up veth interfaces ... Done"
      for qbr in `ifconfig -a | grep qbr | cut -d' ' -f1`
      do
        `sudo ip link set $qbr down` || true
        `sudo ip link delete $qbr` || true
      done
      echo `date` bsn-nova-init "Cleaning up bridges ... Done"
    fi

    /usr/bin/quantum-ovs-cleanup || :

    if [ -f /etc/init/nova-compute.conf ] ; then
     /usr/sbin/service nova-compute start || :
     sleep 3
     sed -i '$d' /etc/nova/nova.conf
    fi

    if [ -f /etc/init/quantum-dhcp-agent.conf ] ; then
      /usr/sbin/service quantum-dhcp-agent start || :
    fi

    echo `date` bsn-nova-init "Cleaning up tuntap interfaces ... Done"
  fi

  echo `date` bsn-nova-init "Started ... Done"
end script
EOF

# Done
echo "$0 Done."
echo