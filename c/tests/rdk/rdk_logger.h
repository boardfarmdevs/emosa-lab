/* A stub of rdk-logger's interface (rdk_logger.h of rdk-logger 2.4.0, the part log.c
 * uses), for c/tests/log_rdk.c: the same names, types and values; the calls are recorded
 * by the test, which defines the functions. */
#ifndef _RDK_LOGGER_H_
#define _RDK_LOGGER_H_

#include <stdarg.h>
#include <stdint.h>

typedef uint32_t rdk_Error;
#define RDK_SUCCESS 0

typedef enum {
    RDK_LOG_FATAL = 0,
    RDK_LOG_ERROR,
    RDK_LOG_WARN,
    RDK_LOG_NOTICE,
    RDK_LOG_INFO,
    RDK_LOG_DEBUG,
    RDK_LOG_TRACE,
    RDK_LOG_NONE
} rdk_LogLevel;

#define RDK_LOGGER_EXT_FILENAME_SIZE 32
#define RDK_LOGGER_EXT_LOGDIR_SIZE 32

typedef struct rdk_logger_ext_config_t {
    char fileName[RDK_LOGGER_EXT_FILENAME_SIZE];
    char logdir[RDK_LOGGER_EXT_LOGDIR_SIZE];
    long maxSize;
    long maxCount;
} rdk_logger_ext_config_t;

rdk_Error rdk_logger_ext_init(const rdk_logger_ext_config_t *config);
void rdk_logger_msg_printf(rdk_LogLevel level, const char *module, const char *format, ...)
    __attribute__((format(printf, 3, 4)));

#define RDK_LOG rdk_logger_msg_printf

#endif
