# Operator and team guides

[Documentation index](../README.md)

| Guide | Use it to |
| --- | --- |
| [Beginner’s team and operator manual](team-manual.md) | Learn the concepts and purpose of each step, establish a checkout, interpret results, operate every supported experiment and deliver scoped demos |
| [Connecting-pod walkthrough](connecting-pod.md) | Run a simulated extender that initiates OVSDB to EMOSA, inspect its diagnostic virtual agent, apply configuration and demonstrate recovery |
| [Service integration walkthrough](service-integration.md) | Exercise two pods, restore owned radios after reboot, test the actual service with hwsim clients, and prepare the live controller trial |
| [Onboarding readiness checks](onboarding-readiness.md) | Check complete radio/BSS scope and review the native controller's captured profiles and WSC payload set |
| [Offline EasyMesh payload exercise](../protocol/easymesh-payloads.md) | Decode/build selected values and reproduce their independent native-capture checks without a VM or pod |
| [Stable identities and observed topology](observed-topology.md) | Bind every simulated radio/VIF, inspect a complete State-derived AP value, and test reconnect/row recreation/service restart |
| [Radio capability inputs](radio-capabilities.md) | Supply explicit evidence-backed synthetic limits, inspect per-radio values, and test withdrawal on changed inputs or observations |
| [Profile-readiness walkthrough](../protocol/profile-readiness.md) | Distinguish a working component from a qualified profile, audit feature conditions and inspect feature/counter-unit values offline |
| [Technology and Device Inventory](technology-inventory.md) | Map explicit HT/VHT and inventory claims through two simulated pods; understand opaque HE and separate readiness results |
| [HE MCS and Wi-Fi 6 capability values](he-wifi6.md) | Inspect direction/width/role fields, reproduce a native negative case and understand remaining report/peer gaps |
| [Read-only physical-pod qualification](pod-qualification.md) | Prepare private local connection inputs, collect an actual schema/inventory and supply useful root-pod/cloud captures |

For the distinction between the adapter, its virtual-agent role and the native
prplMesh peers, read [manual §2.5](team-manual.md#25-languages-and-upstream-reuse).
It also identifies the actual Python service, upstream C/C++ components, where
native binaries come from, and which experiments require those artifacts.

For deployment design, see [one service managing several pods](team-manual.md#26-one-adapter-service-several-represented-pods)
and [cloud, EasyMesh, EMOSA and ODH flows](team-manual.md#27-compare-cloud-easymesh-and-emosa-connection-flows),
including the [visual comparison](../architecture/connection-flows.svg). ODH is the
data lake in the network center; its ingestion contract remains an input to define.

Start on the development host. The model and ordinary OVSDB exercises need no
LXD or radio. Follow the team manual's explicit HOST/VM/CONTAINER labels before
using the prepared native or radio lab. Private pod credentials and raw physical
captures belong outside the repository.
