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

    # Optional: mirror defect_detection/results to a remote broker (e.g. on OpenShift SNO)
    # for higher-level analysis, tolerant of that broker being intermittently offline.
    # Opt-in via FEDERATION_HOST so a plain local broker isn't left with a bridge
    # endlessly retrying a target that was never configured.
    #
    # This uses a Core Bridge, not Artemis Federation -- the arkmq-org-broker image does
    # not ship the federation module at all (verified: no federation-related class/jar
    # anywhere in the image), despite broker.xml schema validation silently accepting
    # <federations> config that then does nothing at runtime. Core Bridges are a base
    # artemis-server feature and were verified end-to-end locally: live forwarding, and
    # -- the actual point of this -- messages queue durably here while the target is
    # unreachable and flush automatically once it reconnects, with no message loss.
    #
    # IMPORTANT: the bridge does a pre-flight query against the remote broker for the
    # forwarding-address's bindings before it will ever report Connected=true. auto-create
    # on the remote does NOT satisfy this (auto-create only fires on an actual send, not a
    # query) -- the remote side needs "defect_detection.results" pre-created as a MULTICAST
    # address with a queue bound to it. See the OpenShift-side manifests for that.
    if [ -n "$FEDERATION_HOST" ]; then
        FEDERATION_PORT="${FEDERATION_PORT:-443}"

        sed -i "s|<acceptors>|<connectors>\n         <connector name=\"federation-connector\">tcp://${FEDERATION_HOST}:${FEDERATION_PORT}?sslEnabled=true;trustStorePath=/etc/artemis-federation-tls/truststore.p12;trustStorePassword=${FEDERATION_TRUSTSTORE_PASSWORD};trustStoreType=PKCS12</connector>\n      </connectors>\n\n      <acceptors>|" \
            broker/etc/broker.xml

        sed -i "s|</acceptors>|</acceptors>\n\n      <bridges>\n         <bridge name=\"results-bridge\">\n            <queue-name>federation.results.queue</queue-name>\n            <forwarding-address>defect_detection.results</forwarding-address>\n            <routing-type>MULTICAST</routing-type>\n            <static-connectors>\n               <connector-ref>federation-connector</connector-ref>\n            </static-connectors>\n            <user>${AMQ_USER}</user>\n            <password>${AMQ_PASSWORD}</password>\n         </bridge>\n      </bridges>|" \
            broker/etc/broker.xml

        sed -i "s|</addresses>|   <address name=\"defect_detection.results\">\n         <multicast>\n            <queue name=\"federation.results.queue\"/>\n         </multicast>\n      </address>\n\n   </addresses>|" \
            broker/etc/broker.xml
    fi
fi

exec broker/bin/artemis run