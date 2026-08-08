#!/bin/sh
# Creates the Artemis broker instance on first run against an empty data volume, then
# runs it in the foreground. Re-running against a volume that already has an instance
# just starts it, skipping create.
#
# This guard is necessary because the image's default launch.sh only runs "artemis
# create" when its instance directory (./broker, relative to $HOME=/home/jboss) doesn't
# exist yet -- but podman (like Kubernetes) pre-creates that directory as soon as a
# volume is mounted there, even empty, which fools launch.sh into thinking an instance
# already exists. It then skips creation and crash-loops on a missing bin/artemis.
# Checking for the actual "artemis" binary instead of the directory avoids that trap.
set -e
cd /home/jboss

if [ -x broker/bin/artemis ]; then
    echo "Broker instance already present in the data volume, skipping create"
else
    /opt/amq/bin/artemis create broker \
        --role admin --name broker \
        --allow-anonymous \
        --user "$AMQ_USER" --password "$AMQ_PASSWORD" \
        --host 0.0.0.0 --http-host 0.0.0.0 \
        --force

    # "--http-host 0.0.0.0" makes `artemis create` seed the console's CORS allowlist with
    # <allow-origin>*://0.0.0.0*</allow-origin> -- but no browser ever actually visits
    # http://0.0.0.0:8161 (that's a bind-all address, not a real client origin), so with
    # <strict-checking/> enabled this rejects every real request with a 403 ("Origin ...
    # is not allowed to call this agent"), and the Hawtio console loads but stays blank.
    # Broadening it to a full wildcard matches this repo's existing trust model (anonymous
    # MQTT, no TLS anywhere) rather than guessing at a specific origin/subnet to allow.
    sed -i 's|<allow-origin>\*://0.0.0.0\*</allow-origin>|<allow-origin>*</allow-origin>|' \
        broker/etc/jolokia-access.xml
fi

exec broker/bin/artemis run