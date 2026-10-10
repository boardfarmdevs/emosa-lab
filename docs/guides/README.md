# Guides

[Documentation index](../README.md)

| Guide | Use it to |
| --- | --- |
| [Physical pods in the labs](physical-pods.md) | Bring an operator-provisioned pod, including a years-old one, into opensync-lab and then EMOSA: provisioning asks, Ethernet attachment, cloud trust, profile and data plane |
| [Read-only physical-pod qualification](pod-qualification.md) | Prepare private local connection inputs, collect an actual schema/inventory and supply useful root-pod/cloud captures |
| [The bench CA](bench-ca.md) | Set up the mutual TLS of the pods' statistics on a physical bench: one CA whose key stays on its host, keys made on the router and the pods |

The prototype phase's manual, learning path and experiment guides were removed on 8 October
2026 (the owner's decision); they are in this repository's history. What they taught is now
the [specification](../../spec/README.md), the [handover](../handover/README.md) and the
[lab in a box](../../spec/box-scenarios.md); the scripts and examples they described stay
where they are.
