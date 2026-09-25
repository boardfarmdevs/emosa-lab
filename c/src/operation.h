/* Operation states and their legal transitions (spec §5), as emosa.operations. */
#ifndef EMOSA_OPERATION_H
#define EMOSA_OPERATION_H

#include <stdbool.h>

typedef enum {
    EM_OP_REQUESTED, EM_OP_VALIDATED, EM_OP_SUBMITTED, EM_OP_CONFIG_COMMITTED,
    EM_OP_INDETERMINATE, EM_OP_OBSERVED_APPLIED, EM_OP_REJECTED, EM_OP_FAILED,
    EM_OP_OWNERSHIP_CONFLICT, EM_OP_TIMED_OUT, EM_OP_CANCELLED, EM_OP_COUNT
} em_op_state;

const char *em_op_state_name(em_op_state s);
bool em_op_active(em_op_state s);
bool em_op_may_transition(em_op_state from, em_op_state to);

#endif
