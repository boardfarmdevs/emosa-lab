/* SPDX-License-Identifier: Apache-2.0 */
/* Runs a fuzz target over files, for builds without libFuzzer (gcc): each
 * argument is a file or a directory of files, each file one input. CI replays the
 * seed corpora this way under the sanitizers; libFuzzer builds (clang,
 * -DEMOSA_FUZZ=ON) search from the same seeds. */
#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static int run_file(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        perror(path);
        return 1;
    }
    uint8_t *data = NULL;
    size_t size = 0, cap = 0, n;
    uint8_t chunk[4096];
    while ((n = fread(chunk, 1, sizeof(chunk), f)) > 0) {
        if (size + n > cap) {
            cap = (size + n) * 2;
            uint8_t *grown = realloc(data, cap);
            if (!grown) {
                free(data);
                fclose(f);
                return 1;
            }
            data = grown;
        }
        memcpy(data + size, chunk, n);
        size += n;
    }
    fclose(f);
    LLVMFuzzerTestOneInput(data ? data : (const uint8_t *)"", size);
    free(data);
    return 0;
}

int main(int argc, char **argv)
{
    int failures = 0, inputs = 0;
    for (int i = 1; i < argc; i++) {
        struct stat st;
        if (stat(argv[i], &st) != 0) {
            perror(argv[i]);
            return 1;
        }
        if (!S_ISDIR(st.st_mode)) {
            failures += run_file(argv[i]);
            inputs++;
            continue;
        }
        DIR *d = opendir(argv[i]);
        struct dirent *e;
        while (d && (e = readdir(d))) {
            if (e->d_name[0] == '.')
                continue;
            char path[4096];
            snprintf(path, sizeof(path), "%s/%s", argv[i], e->d_name);
            failures += run_file(path);
            inputs++;
        }
        if (d)
            closedir(d);
    }
    printf("%d inputs, %d unreadable\n", inputs, failures);
    return failures ? 1 : 0;
}
