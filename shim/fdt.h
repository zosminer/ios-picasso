#pragma once
#include <stdint.h>

/* Read a u32/u64 (cell-size agnostic) property of /chosen. Returns 0 if absent. */
int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out);
/* picasso 新增：读取 /memory 节点的 DRAM 区段（基址, 大小），返回区段数。 */
int fdt_memory_banks(uintptr_t fdt, uint64_t *base, uint64_t *size, int max);
/* picasso 新增：名字以 prefix 开头的第一个节点的 reg 第一项；找到返回 1。 */
int fdt_node_reg(uintptr_t fdt, const char *prefix, uint64_t *base, uint64_t *size);
