#!/bin/bash
# RVC Realtime Voice Changer - Web UI (behind an nginx reverse proxy)
#
# For the setup where nginx runs on ANOTHER machine on the LAN and handles
# TLS plus authentication for anyone coming in from the internet.
#
#   --no-token   access control is nginx's job, not the app's
#   --trust-lan  required because a non-loopback bind means this port can be
#                reached directly, bypassing nginx. Only correct while nothing
#                forwards port 7866 in from the internet - nginx's 443 should
#                be the only thing exposed.
#
# If you ever move nginx onto this Mac, switch to:
#   --host 127.0.0.1 --no-token      (and drop --trust-lan)

cd "$(dirname "$0")"
source .venv/bin/activate
export PYTORCH_ENABLE_MPS_FALLBACK=1
python -u -m webui.server --host 0.0.0.0 --port 7866 --no-token --trust-lan
