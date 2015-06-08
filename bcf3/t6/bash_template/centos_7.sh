#!/bin/bash

# Make sure only root can run this script
if [ "$(id -u)" != "0" ]; then
   echo -e "Please run as root" 
   exit 1
fi

install_bsnstacklib=%(install_bsnstacklib)s
install_ivs=%(install_ivs)s
install_all=%(install_all)s
deploy_dhcp_agent=%(deploy_dhcp_agent)s
ivs_version=%(ivs_version)s
is_controller=%(is_controller)s
deploy_horizon_patch=%(deploy_horizon_patch)s

# prepare dependencies
set +e
rpm -iUvh http://dl.fedoraproject.org/pub/epel/7/x86_64/e/epel-release-7-5.noarch.rpm
rpm -ivh https://yum.puppetlabs.com/el/7/products/x86_64/puppetlabs-release-7-10.noarch.rpm
yum groupinstall -y 'Development Tools'
yum install -y python-devel puppet python-pip wget libffi-devel openssl-devel
yum update -y

# install bsnstacklib
if [[ $install_bsnstacklib == true ]]; then
    pip install --upgrade "bsnstacklib<%(bsnstacklib_version)s"
fi

# install ivs
if [[ $install_ivs == true ]]; then
    # check ivs version compatability
    pass=true
    ivs --version
    if [[ $? == 0 ]]; then
        old_version=$(ivs --version | awk '{print $2}')
        old_version_numbers=(${old_version//./ })
        new_version_numbers=(${ivs_version//./ })
        if [[ ${old_version_numbers[0]} == 0 ]]; then
            pass=true
        elif [[ "$old_version" != "${old_version%%$ivs_version*}" ]]; then
            pass=true
        elif [[ $old_version > $ivs_version ]]; then
            pass=false
        elif [[ $((${new_version_numbers[0]}-1)) -gt ${old_version_numbers[0]} ]]; then
            pass=false
        fi
    fi

    if [[ $pass == true ]]; then
        rpm -ivh --force %(dst_dir)s/%(ivs_pkg)s
        if [[ -f %(dst_dir)s/%(ivs_debug_pkg)s ]]; then
            rpm -ivh --force %(dst_dir)s/%(ivs_debug_pkg)s
        fi
    else
        echo "ivs upgrade fails version validation"
    fi
fi

# full installation of bcf
if [[ $install_all == true ]]; then
    puppet module install --force puppetlabs-inifile
    puppet module install --force puppetlabs-stdlib
    puppet module install jfryman-selinux
    mkdir -p /etc/puppet/modules/selinux/files
    cp %(dst_dir)s/%(hostname)s.te /etc/puppet/modules/selinux/files/centos.te
    cp /usr/lib/systemd/system/neutron-openvswitch-agent.service /usr/lib/systemd/system/neutron-bsn-agent.service

    # remove ovs, example ("br-storage" "br-prv" "br-ex")
    declare -a ovs_br=(%(ovs_br)s)
    len=${#ovs_br[@]}
    for (( i=0; i<$len; i++ )); do
        ovs-vsctl del-br ${ovs_br[$i]}
    done

    # delete ovs br-int
    while true; do
        ovs-vsctl del-br %(br-int)s
        ovs-vsctl show | grep %(br-int)s
        if [[ $? != 0 ]]; then
            break
        fi
    done

    # deploy bcf
    puppet apply --modulepath /etc/puppet/modules %(dst_dir)s/%(hostname)s.pp

    # assign ip to ivs internal ports
    bash /etc/rc.d/rc.local

    # chmod neutron config since bigswitch horizon patch reads neutron config as well
    chmod -R a+r /usr/share/neutron
    chmod -R a+x /usr/share/neutron
    chmod -R a+r /etc/neutron
    chmod -R a+x /etc/neutron

    # deploy bcf horizon patch to controller node
    if [[ $is_controller == true && $deploy_horizon_patch == true ]]; then
        if [[ -f %(dst_dir)s/%(horizon_patch)s ]]; then
            chmod -R 777 '/etc/neutron/'
            tar -xzf %(dst_dir)s/%(horizon_patch)s -C %(dst_dir)s
            fs=('openstack_dashboard/dashboards/admin/dashboard.py' 'openstack_dashboard/dashboards/project/dashboard.py' 'openstack_dashboard/dashboards/admin/connections' 'openstack_dashboard/dashboards/project/connections')
            for f in "${fs[@]}"
            do
                if [[ -f %(dst_dir)s/%(horizon_patch_dir)s/$f ]]; then
                    yes | cp -rfp %(dst_dir)s/%(horizon_patch_dir)s/$f %(horizon_base_dir)s/$f
                else
                    yes | cp -rfp %(dst_dir)s/%(horizon_patch_dir)s/$f/* %(horizon_base_dir)s/$f
                fi
            done
            find "%(horizon_base_dir)s" -name "*.pyc" -exec rm -rf {} \;
            systemctl restart httpd
        fi
    fi

    # patch linux/dhcp.py to make sure static host route is pushed to instances
    dhcp_py=$(find /usr -name dhcp.py | grep linux)
    dhcp_dir=$(dirname "${dhcp_py}")
    sed -i 's/if (isolated_subnets\[subnet.id\] and/if (True and/g' $dhcp_py
    find $dhcp_dir -name "*.pyc" -exec rm -rf {} \;
    find $dhcp_dir -name "*.pyo" -exec rm -rf {} \;
    if [[ $deploy_dhcp_agent == true ]]; then
        echo 'Restart neutron-metadata-agent and neutron-dhcp-agent'
        systemctl restart neutron-metadata-agent
        systemctl enable neutron-metadata-agent
        systemctl restart neutron-dhcp-agent
        systemctl enable neutron-dhcp-agent
    else
        echo 'Stop and disable neutron-metadata-agent and neutron-dhcp-agent'
        systemctl stop neutron-metadata-agent
        systemctl disable neutron-metadata-agent
        systemctl stop neutron-dhcp-agent
        systemctl disable neutron-dhcp-agent
    fi
fi

# restart libvirtd and nova compute on compute node
if [[ $is_controller == false ]]; then
    echo 'Restart libvirtd and openstack-nova-compute'
    systemctl restart libvirtd
    systemctl enable libvirtd
    systemctl restart openstack-nova-compute
    systemctl enable openstack-nova-compute
fi

# restart neutron-server on controller node
if [[ $is_controller == true ]]; then
    echo 'Restart neutron-server'
    rm -rf /etc/neutron/plugins/ml2/host_certs/*
    systemctl restart neutron-server
fi

set -e

