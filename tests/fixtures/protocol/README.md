# Protocol fixtures — pending P0

The [EasyMesh value fixtures](easymesh/README.md) contain selected native-peer
values independently extracted and interpreted by Wireshark. Their exact byte
checks supplement the selected EasyMesh field tables; they do not qualify whole
IEEE messages or turn native traffic into an EMOSA exchange.

The [WSC component vectors](wsc/README.md) are independently generated synthetic
cryptographic payload fragments under WPS 2.0.10. They are not full wire messages
or onboarding evidence. IEEE base/amendment access, the complete selected
procedure matrix and independent packet vectors remain pending.
`doc/protocol/protocol-matrix.json` records the blocked procedures. Selecting the wire
suite fails explicitly; the runner emits a blocked report and performs no write.
