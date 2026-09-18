#!/usr/bin/env bash
# smb-share.sh - share ~/recordings over SMB so macOS Finder can mount it and
# drag-and-drop takes (Finder speaks SMB natively; no macFUSE/sshfs needed).
#
#   sudo ./smb-share.sh              # open share, no password (default)
#   sudo GUEST=0 ./smb-share.sh      # password-protected (prompts to set one)
#
# Then on the Mac: Finder > Go > Connect to Server (Cmd-K) >
#   smb://10.55.0.1/recordings        (USB-C gadget link, fastest)
#   smb://<lan-ip>/recordings         (wifi, slower - same share)
# ...and click "Guest" when asked (or use the radxa account with GUEST=0).
#
# Open share = anyone who can reach the board can read/delete takes. That is
# fine for a home LAN and a direct USB-C cable; use GUEST=0 on untrusted
# networks.
#
# The share lives in its own include file, so re-running this replaces it
# cleanly instead of appending duplicates to smb.conf.
set -euo pipefail

USER_NAME=${USER_NAME:-radxa}
SHARE_DIR=${SHARE_DIR:-/home/$USER_NAME/recordings}
GUEST=${GUEST:-1}
INC=/etc/samba/rock-recordings.conf

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

apt-get install -y samba

# an earlier version of this script appended the stanza straight into
# smb.conf; strip it so the include file below is the only definition
if grep -q '^\[recordings\]' /etc/samba/smb.conf; then
  cp /etc/samba/smb.conf /etc/samba/smb.conf.bak.$(date +%s)
  sed -i '/^\[recordings\]/,/^\[[^]]*\]$/{ /^\[recordings\]/d; /^\[[^]]*\]$/!d }' \
      /etc/samba/smb.conf
  echo "removed the previously inlined [recordings] stanza (backup kept)"
fi

mkdir -p "$SHARE_DIR"
chown "$USER_NAME:$USER_NAME" "$SHARE_DIR"

if [ "$GUEST" = "1" ]; then
  cat > "$INC" <<EOF
[recordings]
   comment = ROCK 5T stereo takes
   path = $SHARE_DIR
   browseable = yes
   read only = no
   guest ok = yes
   guest only = yes
   force user = $USER_NAME
   create mask = 0644
   directory mask = 0755

# samba publishes every account's home folder by default, so connecting as
# Guest offers the guest account's home ("nobody") - a share that does not
# exist and errors when opened. This include sits last in smb.conf, so these
# values override the stock [homes] section.
[homes]
   available = no
   browseable = no
EOF
  # unknown users become the guest account instead of being rejected
  grep -qi '^[[:space:]]*map to guest' /etc/samba/smb.conf \
    || sed -i '0,/^\[global\]/s//[global]\n   map to guest = Bad User/' /etc/samba/smb.conf
  MODE="OPEN (no password - connect as Guest)"
else
  cat > "$INC" <<EOF
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

# samba publishes every account's home folder by default, so connecting as
# Guest offers the guest account's home ("nobody") - a share that does not
# exist and errors when opened. This include sits last in smb.conf, so these
# values override the stock [homes] section.
[homes]
   available = no
   browseable = no
EOF
  MODE="password (user $USER_NAME)"
fi

grep -q "include = $INC" /etc/samba/smb.conf || printf '\ninclude = %s\n' "$INC" >> /etc/samba/smb.conf

if [ "$GUEST" != "1" ]; then
  echo
  echo "Set the SMB password for '$USER_NAME' (separate from the login password):"
  smbpasswd -a "$USER_NAME"
fi

testparm -s >/dev/null 2>&1 || { echo "samba config check FAILED - see testparm"; exit 1; }
systemctl enable smbd >/dev/null 2>&1 || true
systemctl restart smbd

echo
echo "Share ready - $MODE"
echo "In Finder press Cmd-K and connect to:"
echo "   smb://10.55.0.1/recordings                        (USB-C cable)"
echo "   smb://$(hostname -I | awk '{print $1}')/recordings   (network)"
