# Synthetic model identities

Tests deliberately use `pod-1`, `radio-1`, `bss-1`, `initial-network`, and
separately generated private test credentials. These identities have no physical
device association. `tests/test_model.py` controls the clock and device feedback;
`tests/test_evaluation.py` verifies repeatable phase order with a fixed seed.
