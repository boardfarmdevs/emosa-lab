# The bench CA

[Guides](README.md)

The pods' statistics (spec §3.6) travel over MQTT with mutual TLS: each pod presents a device
certificate to the broker on its router, and checks the broker's certificate. On a physical bench
one CA vouches for both. This guide sets it up with `deploy/bench-ca/bench-ca.sh`.

## The rules

- **The CA key stays on the CA host.** It is made there and never copied to a router, a pod or a
  repository. The script refuses to run on any other host (`BENCH_CA_HOST`).
- **Keys are made where they are used.** The broker's key is made on the router, and a pod's on the
  pod. Only certificate requests (CSRs) travel to the CA host, and only certificates and `ca.pem`
  travel back.
- **A router holds `ca.pem` and its broker certificate and key. A pod holds `ca.pem` and its device
  certificate and key.** Nothing else from the CA.
- **A pod's certificate is named by its serial** (`CN=<serial>` and nothing else): the broker takes
  that as the pod's user name (`use_identity_as_username`), and the pod publishes on its own topic.

Certificates last 825 days and the CA 10 years. `bench-ca.sh list` shows what was issued and when
it expires.

## Once, on the CA host

In a shell on the CA host, in a checkout of this repository:

```sh
export BENCH_CA_HOST=$(hostname -s)
deploy/bench-ca/bench-ca.sh init
```

The CA lives in `~/bench-ca` (mode 0700; `BENCH_CA_DIR` to choose another). If its key is lost,
run `init` again in a new directory and issue every certificate again.

## The router's broker

1. **On the router** (its image may do this itself): make the broker's key and request. The
   subject is the address the pods connect to, the router's LAN address:

   ```sh
   umask 077
   mkdir -p /etc/mosquitto/certs
   openssl req -new -newkey rsa:2048 -nodes -keyout /etc/mosquitto/certs/broker.key \
       -subj "/CN=192.168.1.1" -out /tmp/broker.csr
   ```

2. **On the CA host**: fetch the request, sign it for every address the pods use, and send back the
   certificate and `ca.pem`:

   ```sh
   scp root@192.168.1.1:/tmp/broker.csr .
   deploy/bench-ca/bench-ca.sh sign-broker broker.csr broker.pem 192.168.1.1
   deploy/bench-ca/bench-ca.sh ca > ca.pem
   scp broker.pem ca.pem root@192.168.1.1:/etc/mosquitto/certs/
   ```

3. **On the router**: the broker's listener for the pods, as the RDK lab's gateway configures it
   (`deploy/rdk-lab/vm/gateway.sh`, `gateway_broker`):

   ```
   per_listener_settings true
   listener 8883 192.168.1.1
   cafile /etc/mosquitto/certs/ca.pem
   certfile /etc/mosquitto/certs/broker.pem
   keyfile /etc/mosquitto/certs/broker.key
   require_certificate true
   use_identity_as_username true
   listener 1883 127.0.0.1
   allow_anonymous true
   ```

   The second listener is the agents' (spec §3.6: they read the pods' topics on the loopback).

## Each pod

1. **On the pod** (its image may do this itself): make its key and request, named by its serial,
   where OpenSync takes its device certificate (`/var/certs`):

   ```sh
   umask 077
   serial=$(ovsh -r s AWLAN_Node serial_number)
   openssl req -new -newkey rsa:2048 -nodes -keyout /var/certs/client_dec.key \
       -subj "/CN=$serial" -out /tmp/pod.csr
   ```

2. **On the CA host**: fetch, sign and send back:

   ```sh
   scp root@POD:/tmp/pod.csr .
   deploy/bench-ca/bench-ca.sh sign-pod pod.csr client.pem
   deploy/bench-ca/bench-ca.sh ca > ca.pem
   scp client.pem ca.pem root@POD:/var/certs/
   ```

   `sign-pod` refuses a request whose subject is not `CN=<serial>` alone, and a weak key (RSA
   under 2048 bits, EC other than P-256 or P-384).

The agent then sets the pod's MQTT settings (telemetry `mode mqtt`, `broker` the router's LAN
address) and the pod connects with its certificate.

## Retiring a pod

There is no revocation list. To shut a pod out before its certificate expires, deny its user name
(its serial) in the broker's ACL (`acl_file`). Retiring the whole bench means a new CA.
