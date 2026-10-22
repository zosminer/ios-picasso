#pragma once
#include <stdint.h>

/* Boot the SWEETXNU payload found in the ABL ramdisk; returns only on error.
 * picasso：dtb 就是 ABL 给的 x0；它不是设备树时，loader 会在 x0 附近的 ABL 堆里找负载。 */
const char *loader_boot(uintptr_t dtb);
