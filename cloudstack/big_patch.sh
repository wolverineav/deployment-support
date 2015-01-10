#!/bin/bash
#
# Copyright 2014 Big Switch Networks, Inc.
# All Rights Reserved.
#
# This script is used to set up cloud stack management node
# and compute nodes with Big Cloud Fabric. The requirements are:
# BCF 2.5
# installation node: ubuntu 12.04, centos 6.5 or centos 6.6
# management node: ubuntu 12.04, centos 6.5 or centos 6.6
# compute node: ubuntu 12.04 centos6.5, centos 6.6 or xenserver 6.2
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
# please also download example.yaml and make necessary modifications
# according to your physical setup.
#
# Usage: bash big_patch.sh example.yaml

# check configuration file
if [[ -z "$*" ]]; then
    echo -e "No configuration file is specified.\nUsage: bash big_patch.sh example.yaml"
    exit 1
fi
config=$1
if [[ ! -f ${config} ]]; then
    echo "Configuration file does not exist"
    exit 1
fi
echo "Configuration file is ${config}"
mkdir -p /home/root/bcf
cp ${config} /home/root/bcf/

rm -rf ~/.ssh/known_hosts
rm -f /var/log/cloudstack_deploy.log

# if os is ubuntu
python -mplatform | grep Ubuntu
if [[ $? == 0 ]]; then
    sudo apt-get update -y
    sudo apt-get -fy install --fix-missing
    sudo apt-get install -y sshpass python-yaml python-pip python-dev
    sudo pip install futures subprocess32
    rm -f /home/root/bcf/big_patch.py
    wget --no-check-certificate https://raw.githubusercontent.com/bigswitch/deployment-support/bcf-2.5/cloudstack/big_patch.py -P /home/root/bcf
    python /home/root/bcf/big_patch.py -c /home/root/bcf/${config}
    exit 0
fi

# if os is centos
python -mplatform | grep centos
if [[ $? == 0 ]]; then
    yum update -y

    # install sshpass
    rm -f /home/root/bcf/sshpass-1.05-1.el6.rf.x86_64.rpm
    wget http://pkgs.repoforge.org/sshpass/sshpass-1.05-1.el6.rf.x86_64.rpm -P /home/root/bcf/
    yum install -y /home/root/bcf/sshpass-1.05-1.el6.rf.x86_64.rpm

    # install epel repo
    rm -f /home/root/bcf/epel-release-5-4.noarch.rpm
    wget http://dl.fedoraproject.org/pub/epel/5/x86_64/epel-release-5-4.noarch.rpm -P /home/root/bcf/
    yum install -y /home/root/bcf/epel-release-5-4.noarch.rpm
    yum update -y

    # install python 2.7
    yum install -y centos-release-SCL
    yum install -y python27
    scl enable python27 "
    yum install -y gcc
    yum groupinstall -y 'Development Tools'
    easy_install pyyaml;
    easy_install subprocess32;
    rm -f /home/root/bcf/big_patch.py;
    wget --no-check-certificate https://raw.githubusercontent.com/bigswitch/deployment-support/bcf-2.5/cloudstack/big_patch.py -P /home/root/bcf/;
    python /home/root/bcf/big_patch.py -c /home/root/bcf/${config}"
    exit 0
fi

# we don't support other os
echo "Please use centos 6.5, 6.6 or ubuntu 12.04 as cloudstack management node"

