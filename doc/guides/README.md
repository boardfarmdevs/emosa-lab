# Operator and team guides

[Documentation index](../README.md)

| Guide | Use it to |
| --- | --- |
| [Beginner’s team and operator manual](team-manual.md) | Learn the concepts and purpose of each step, establish a checkout, interpret results, operate every supported experiment and deliver scoped demos |
| [Connecting-pod walkthrough](connecting-pod.md) | Run a simulated extender that initiates OVSDB to EMOSA, inspect its diagnostic virtual agent, apply configuration and demonstrate recovery |
| [Service integration walkthrough](service-integration.md) | Exercise two pods, restore owned radios after reboot, test the actual service with hwsim clients, and prepare the live controller trial |
| [Onboarding readiness checks](onboarding-readiness.md) | Check complete radio/BSS scope and review the native controller's captured profiles and WSC payload set |
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
