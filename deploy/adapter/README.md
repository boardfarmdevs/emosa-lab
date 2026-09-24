# EMOSA adapter kit

The kit installs EMOSA next to an existing EasyMesh controller. Each OpenSync
pod handed to it appears at that controller as an EasyMesh agent. The adapter's
behaviour is specified in [`spec/`](../../spec/README.md).

## What the lab must provide

1. **An L2 path to the controller's EasyMesh LAN.** The host or container
   needs one interface on the segment where the controller sends and receives
   IEEE 1905.1 frames (EtherType `0x893A`), for example a veth or NIC on the
   controller's bridge. This is the *trunk*. Each agent adds a macvlan on it.
2. **The controller's AL MAC address**, and which message set it speaks:

   | Controller | `message_set` | `multi_bss` | `m2_session` |
   | --- | --- | --- | --- |
   | prplMesh 6.0 | `easymesh-6.1` | `false` (or `true`) | `distinct` |
   | RDK unified-wifi-mesh | `r1` | `true` | `shared` |

3. **A way to hand pods over.** The pods' cloud side (an OpenSync redirector,
   or opensync-lab's `local-noc`) sets each chosen pod's `manager_addr` to
   EMOSA's front port, `tcp:<advertise>:6650`. The pods reach the front port
   and the agent ports (6651 and up) at the `advertise` address.

If the controller only configures agents it knows, add each agent's AL MAC to
the controller's onboarding policy. The fleet prints the AL MAC, and
`emosa-fleet list` shows all of them.

## Build and install

```sh
deploy/adapter/build.sh                       # -> dist/emosa-adapter-<version>.tar.gz
# on the target (Ubuntu 24.04 or similar; root; systemd, iproute2, build-essential)
tar -xzf emosa-adapter-<version>.tar.gz && emosa-adapter-<version>/install.sh
```

The installer puts the adapter and its own Python 3.13.7 in
`/opt/emosa-adapter`. It needs network access to PyPI.

## Configure and start

1. In `/etc/default/emosa`, set `EMOSA_TRUNK` to the trunk interface.
2. Edit `/etc/emosa-fleet.json` (schema: `schemas/fleet-config.schema.json`):
   - `advertise`: the address pods use to reach this host;
   - `controller_al`, `message_set`, `multi_bss` and `m2_session`: from the
     table above;
   - `listen` and `ports`: loopback by default. Forward the pods' TCP 6650 and
     up to it, or listen on a reachable address.
3. Start the fleet:

   ```sh
   systemctl enable --now emosa-fleet
   ```

4. Hand pods over as described above. Each one gets `emosa-agent@<serial>`,
   started automatically.

## Watch and operate

```sh
/opt/emosa-adapter/venv/bin/emosa-fleet list /etc/emosa-fleet.json     # every pod and its agent
cat /var/lib/emosa/<serial>/status.json                                 # one agent's view
journalctl -u emosa-fleet -u 'emosa-agent@*'
/opt/emosa-adapter/venv/bin/emosa-fleet forget /etc/emosa-fleet.json <serial>
```

To upgrade, run a newer kit's `install.sh`. It keeps the configuration, the
state and the agents' identities, and restarts running units.

To remove EMOSA, stop and disable the units, then delete `/opt/emosa-adapter`,
`/etc/emosa*`, `/var/lib/emosa`, `/usr/local/sbin/emosa-agent-link` and the two
unit files.
