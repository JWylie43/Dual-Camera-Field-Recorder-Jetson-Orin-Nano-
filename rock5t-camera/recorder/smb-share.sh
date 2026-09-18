#!/usr/bin/env bash
# smb-share.sh - share ~/recordings over SMB so macOS Finder can mount it and
# drag-and-drop takes (Finder speaks SMB natively; no macFUSE/sshfs needed).
#
#   sudo ./smb-share.sh          # install + configure + set the share password
#
# Then on the Mac: Finder > Go > Connect to Server (Cmd-K) >
#   smb://10.55.0.1/recordings     (USB-C gadget link, fastest)
#   smb://192.168.86.136/recordings (wifi, slower - same share)
# Connect as "radxa" with the SMB password set below (it is a SEPARATE password
# from the Linux login one; that is how samba works).
set -euo pipefail

USER_NAME=${USER_NAME:-radxa}
SHARE_DIR=${SHARE_DIR:-/home/$USER_NAME/recordings}

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

apt-get install -y samba

mkdir -p "$SHARE_DIR"
chown "$USER_NAME:$USER_NAME" "$SHARE_DIR"

if ! grep -q '^\[recordings\]' /etc/samba/smb.conf; then
  cat >> /etc/samba/smb.conf <<EOF

[recordings]
   comment = ROCK 5T stereo takes
   path = $SHARE_DIR
   browseable = yes
   read only = no
   guest ok = no
   valid users = $USER_NAME
   force user = $USER_NAME
   create mask = 0644
   directory mask = 0755
EOF
  echo "added [recordings] share to /etc/samba/smb.conf"
else
  echo "[recordings] share already present"
fi

echo
echo "Set the SMB password for '$USER_NAME' (separate from the login password):"
smbpasswd -a "$USER_NAME"

systemctl enable smbd >/dev/null 2>&1 || true
systemctl restart smbd

echo
echo "Share ready. In Finder press Cmd-K and connect to:"
echo "   smb://10.55.0.1/recordings       (USB-C cable)"
echo "   smb://$(hostname -I | awk '{print $1}')/recordings   (network)"
