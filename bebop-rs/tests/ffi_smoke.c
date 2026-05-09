/* C smoke test for the bebop-rs FFI surface.
 *
 * Verifies the static lib can be linked from C, that bebop_init() spins up
 * the embedded Python interpreter without crashing, and that bebop_destroy()
 * cleans up cleanly. Driven by tests/ffi_smoke.rs (Cargo) so we use the
 * same build settings as the rest of the crate.
 *
 * If this passes, the C++ AU shell linking against the same .a will also
 * work — the only difference is C++ vs C compilation flags.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bebop_rs.h"

int main(void) {
    fputs("bebop_init() ... ", stdout);
    fflush(stdout);
    BebopHandle* h = bebop_init();
    if (h == NULL) {
        const char* err = bebop_last_error();
        fprintf(stderr, "FAIL: bebop_init returned NULL (%s)\n",
                err ? err : "no error message");
        return 1;
    }
    puts("ok");

    fputs("bebop_process_audio(zero buffer) ... ", stdout);
    fflush(stdout);
    float zeros[256] = {0};
    int rc = bebop_process_audio(h, zeros, sizeof(zeros)/sizeof(zeros[0]), 22050.0f);
    if (rc != 0) {
        fprintf(stderr, "FAIL: bebop_process_audio returned %d\n", rc);
        return 1;
    }
    puts("ok");

    fputs("bebop_destroy() ... ", stdout);
    fflush(stdout);
    bebop_destroy(h);
    puts("ok");

    fputs("bebop_destroy(NULL) ... ", stdout);
    fflush(stdout);
    bebop_destroy(NULL);
    puts("ok");

    puts("PASS");
    return 0;
}
