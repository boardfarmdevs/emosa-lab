# Protocol fixtures — pending P0

The [WSC component vectors](wsc/README.md) are independently generated synthetic
cryptographic payload fragments under WPS 2.0.10. They are not full wire messages
or onboarding evidence. IEEE base/amendment access, the complete selected
procedure matrix and independent packet vectors remain pending.
`docs/protocol-matrix.json` records the blocked procedures. Selecting the wire
suite fails explicitly; the runner emits a blocked report and performs no write.
