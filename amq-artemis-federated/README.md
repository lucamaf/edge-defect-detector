# Artemis federation target (OpenShift SNO)

This is the OpenShift-side broker that `amq-artemis` (the edge Jetson broker, in
`../amq-artemis/`) mirrors `defect_detection/results` to via a Core Bridge, for
higher-level analysis. It's designed to tolerate this broker being intermittently
offline (e.g. a single-node OpenShift cluster that isn't running all day) -- messages
queue durably on the edge side while this is unreachable, and flush automatically once
it's back. This whole flow (live forwarding, offline queuing, reconnect-flush with zero
message loss) was verified locally with podman before writing these manifests.

**Why a Core Bridge and not Artemis Federation:** the `quay.io/arkmq-org/arkmq-org-broker`
image does not ship the Federation module at all -- confirmed by finding no
federation-related class/jar anywhere in the image. A `<federations>` block in broker.xml
passes schema validation but silently does nothing at runtime. Core Bridges are a base
`artemis-server` feature and work correctly.

**Why the bridge needs `defect_detection.results` pre-created here:** the bridge does a
pre-flight query against this broker for the forwarding address's bindings before it will
ever report `Connected=true`. `auto-create-address` does not satisfy this -- it only
fires on an actual send, not a query. Without the address pre-created (done by the
initContainer in `deployment.yaml`), the bridge sits at `Connected=false` forever,
logging `AMQ222097: Address defect_detection.results does not have any bindings, retry`
in a loop. Verified this failure mode locally, and that pre-creating the address fixes it.

## Setup

**1. Apply the base resources:**

```bash
oc apply -k amq-artemis-federated/
```

The `Deployment`'s pod will stay `Pending`/`ContainerCreating` until you complete step 4
below -- it references a TLS secret that doesn't exist yet. That's expected.

**2. Find the Route's auto-assigned hostname** (no custom host is pinned in
`route-federation.yaml`, so OpenShift assigns one):

```bash
ROUTE_HOST=$(oc get route artemis-federated -o jsonpath='{.spec.host}')
echo "$ROUTE_HOST"
```

**3. Generate a self-signed keystore/truststore pair matching that hostname.** The
acceptor's certificate CN/SAN must match `$ROUTE_HOST` or TLS hostname verification on
the edge side will fail:

```bash
keytool -genkeypair -alias federation -keyalg RSA -keysize 2048 -validity 3650 \
    -keystore keystore.p12 -storetype PKCS12 -storepass CHANGE_ME -keypass CHANGE_ME \
    -dname "CN=${ROUTE_HOST}" -ext "SAN=dns:${ROUTE_HOST}"

keytool -exportcert -alias federation -keystore keystore.p12 -storepass CHANGE_ME \
    -rfc -file federation-cert.pem

keytool -importcert -alias federation -keystore federation-truststore.p12 \
    -storetype PKCS12 -storepass CHANGE_ME -file federation-cert.pem -noprompt
```

**4. Create the keystore secret** (this is what the pod is waiting on):

```bash
oc create secret generic artemis-federation-tls \
    --from-file=keystore.p12=keystore.p12 \
    --from-literal=password=CHANGE_ME
```

The pod should now start. Verify:

```bash
oc logs deployment/artemis-federated -c artemis | grep -E "61617|Server is now active"
```

You should see `Started EPOLL Acceptor at 0.0.0.0:61617 for protocols [CORE]`.

**5. Configure the edge broker** (`amq-artemis` on the Jetson) to federate to this
broker. Copy `federation-truststore.p12` to the Jetson, then set on the edge
`podman run`:

```bash
-e FEDERATION_HOST=$ROUTE_HOST \
-e FEDERATION_PORT=443 \
-e FEDERATION_TRUSTSTORE_PASSWORD=CHANGE_ME \
-v "$(pwd)/federation-truststore.p12:/etc/artemis-federation-tls/truststore.p12:Z" \
```

**Important:** `FEDERATION_PORT=443`, not `61617`. OpenShift passthrough routes are
always reached externally on the router's standard port 443 -- the router uses SNI
matching on the encrypted ClientHello to pick the right backend Service/pod without
decrypting anything, so `61617` only matters as the container's *internal* listening
port, never as what an external client dials.

Re-run (or recreate) the edge `amq-artemis` container after adding those. See
`../amq-artemis/entrypoint.sh` for what it does with them.

## Verifying the federation link

From the edge broker (`amq-artemis`), check the bridge's actual connection state via
Jolokia (same approach used during local testing):

```bash
curl -s -u "$AMQ_USER:$AMQ_PASSWORD" -H "Origin: http://localhost:8161" \
    'http://localhost:8161/console/jolokia/read/org.apache.activemq.artemis:broker="broker",component=bridges,name="results-bridge"/Connected'
```

Should return `"value":true`. To see messages actually arrive here, from this broker:

```bash
oc exec deployment/artemis-federated -c artemis -- \
    /home/jboss/broker/bin/artemis queue stat --url tcp://localhost:61617 \
    --user "$AMQ_USER" --password "$AMQ_PASSWORD"
```

`results.queue` should show a growing `MESSAGES ADDED` count as the edge app publishes
to `defect_detection/results`.