/* Operation states and their legal transitions (spec §5). Mirrors emosa.operations. */
#include "operation.h"

static const char *const NAMES[EM_OP_COUNT] = {
    "REQUESTED", "VALIDATED", "SUBMITTED", "CONFIG_COMMITTED", "INDETERMINATE",
    "OBSERVED_APPLIED", "REJECTED", "FAILED", "OWNERSHIP_CONFLICT", "TIMED_OUT", "CANCELLED"};

const char *em_op_state_name(em_op_state s) { return s < EM_OP_COUNT ? NAMES[s] : "UNKNOWN"; }

bool em_op_active(em_op_state s)
{
    return s == EM_OP_REQUESTED || s == EM_OP_VALIDATED || s == EM_OP_SUBMITTED ||
           s == EM_OP_CONFIG_COMMITTED || s == EM_OP_INDETERMINATE;
}

#define B(s) (1u << (s))
static const unsigned TRANSITIONS[EM_OP_COUNT] = {
    [EM_OP_REQUESTED] = B(EM_OP_VALIDATED) | B(EM_OP_REJECTED) | B(EM_OP_CANCELLED),
    [EM_OP_VALIDATED] = B(EM_OP_SUBMITTED) | B(EM_OP_REJECTED) | B(EM_OP_OWNERSHIP_CONFLICT) |
                        B(EM_OP_CANCELLED) | B(EM_OP_OBSERVED_APPLIED),
    [EM_OP_SUBMITTED] = B(EM_OP_CONFIG_COMMITTED) | B(EM_OP_INDETERMINATE) | B(EM_OP_FAILED) |
                        B(EM_OP_OWNERSHIP_CONFLICT),
    [EM_OP_CONFIG_COMMITTED] = B(EM_OP_OBSERVED_APPLIED) | B(EM_OP_FAILED) | B(EM_OP_TIMED_OUT) |
                               B(EM_OP_OWNERSHIP_CONFLICT),
    [EM_OP_INDETERMINATE] = B(EM_OP_CONFIG_COMMITTED) | B(EM_OP_OBSERVED_APPLIED) |
                            B(EM_OP_OWNERSHIP_CONFLICT) | B(EM_OP_FAILED) | B(EM_OP_TIMED_OUT),
};

bool em_op_may_transition(em_op_state from, em_op_state to)
{
    return from < EM_OP_COUNT && to < EM_OP_COUNT && (TRANSITIONS[from] & B(to));
}
