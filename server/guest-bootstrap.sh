#!/bin/bash
# Only run inside the temporary golden VM created by bootstrap.py.
set -euo pipefail
test -f /root/netlab-build-marker
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io ttyd asciinema sudo curl ca-certificates iproute2
dpkg -i /root/containerlab.deb
systemctl enable --now docker
id lab >/dev/null 2>&1 || useradd -m -s /bin/bash lab
getent group clab_admins >/dev/null || groupadd --system clab_admins
usermod -aG sudo,docker,clab_admins lab
printf 'lab ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/netlab-lab
chmod 440 /etc/sudoers.d/netlab-lab
visudo -cf /etc/sudoers.d/netlab-lab
mkdir -p /opt/network-lab-pristine /home/lab/network-lab
tar xf /root/labs.tar -C /opt/network-lab-pristine
cp -a /opt/network-lab-pristine/. /home/lab/network-lab/
chown -R lab:lab /home/lab/network-lab
bash /root/apply-shell.sh
systemctl enable ttyd
containerlab version
curl --fail --max-time 10 http://127.0.0.1:7681/ -o /dev/null
# Exercise actual containers, links and IP traffic before publishing the image.
# Test the learner's actual shell functions and permissions, not root-only deployment.
sudo -H -u lab bash -ic 'cd "$HOME"; lab 01; rc=$?; [ "$rc" -eq 0 ] || exit "$rc"; n h1 ping -c 2 -W 3 10.10.10.2; rc=$?; unlab || exit $?; exit "$rc"'
# Golden image must boot with unique identity; no credentials or session logs are shipped.
cloud-init clean --logs --machine-id
find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*' -delete
find /var/log/lab-sessions -maxdepth 1 -type f -delete
find /root /home/lab -maxdepth 2 -type f -name authorized_keys -delete
apt-get clean
printf 'NETLAB_GOLDEN_SMOKE_OK\n'
