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
fi

exec broker/bin/artemis run