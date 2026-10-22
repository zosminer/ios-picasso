/*
 * Just enough flattened-device-tree parsing to find what ABL put in
 * /chosen (initrd location). Big-endian, 4-byte aligned tokens.
 */
#include "fdt.h"

#define FDT_MAGIC	0xd00dfeed
#define FDT_BEGIN_NODE	1
#define FDT_END_NODE	2
#define FDT_PROP	3
#define FDT_NOP		4
#define FDT_END		9

static uint32_t be32(uintptr_t a)
{
	return __builtin_bswap32(*(volatile uint32_t *)a);
}

static int streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static uintptr_t align4(uintptr_t a)
{
	return (a + 3) & ~3UL;
}

static uintptr_t skip_str(uintptr_t p)
{
	/* byte loads are fine on Device memory as long as they are aligned to 1 */
	while (*(volatile char *)p)
		p++;
	return p + 1;
}

/* 节点名是否为 base 或 "base@..."（如 memory@80000000） */
static int node_is(const char *name, const char *base)
{
	while (*base && *name == *base)
		name++, base++;
	return !*base && (!*name || *name == '@');
}

/*
 * picasso 新增：读取根节点下所有 "memory" 节点的 reg（ABL 启动时会把真实 DRAM 分布填进去）。
 * 高通平台根节点 #address-cells = #size-cells = 2，所以每项是 4 个 32 位单元（基址 64 位 + 大小 64 位）。
 * 返回读到的区段数，最多 max 个。
 */
int fdt_memory_banks(uintptr_t fdt, uint64_t *base, uint64_t *size, int max)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	uintptr_t strings = fdt + be32(fdt + 12);	/* off_dt_strings */
	int depth = 0, in_memory = 0, n = 0;
	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			in_memory = depth == 2 && node_is(name, "memory");
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			in_memory = 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (in_memory && streq((const char *)(strings + nameoff), "reg")) {
				for (uint32_t off = 0; off + 16 <= len && n < max; off += 16) {
					uint64_t b = (uint64_t)be32(val + off) << 32 | be32(val + off + 4);
					uint64_t s = (uint64_t)be32(val + off + 8) << 32 | be32(val + off + 12);
					if (s) {
						base[n] = b;
						size[n] = s;
						n++;
					}
				}
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END 或数据损坏 */
			return n;
		}
	}
}

/*
 * picasso 新增：找到第一个名字以 prefix 开头的节点（任意层级），读它 reg 的第一项。
 * 支持 2 个单元（32 位地址 + 32 位大小，/soc 下的节点）和 4 个单元（64 位 + 64 位）。
 * 用来读 ABL 运行时才填入的地址，例如 qcom,rmtfs_sharedmem（原厂设备树里是 0）。
 */
int fdt_node_reg(uintptr_t fdt, const char *prefix, uint64_t *base, uint64_t *size)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	uintptr_t strings = fdt + be32(fdt + 12);	/* off_dt_strings */
	int depth = 0, match_depth = -1;
	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			if (match_depth < 0) {
				const char *a = name, *b = prefix;
				while (*b && *a == *b)
					a++, b++;
				if (!*b)
					match_depth = depth;
			}
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			if (depth == match_depth)
				return 0;	/* 找到了节点但没有 reg */
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (depth == match_depth && streq((const char *)(strings + nameoff), "reg")) {
				if (len >= 16) {
					*base = (uint64_t)be32(val) << 32 | be32(val + 4);
					*size = (uint64_t)be32(val + 8) << 32 | be32(val + 12);
				} else if (len >= 8) {
					*base = be32(val);
					*size = be32(val + 4);
				} else {
					return 0;
				}
				return 1;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END 或数据损坏 */
			return 0;
		}
	}
}

int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	uintptr_t strings = fdt + be32(fdt + 12);	/* off_dt_strings */
	int depth = 0, in_chosen = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			in_chosen = depth == 2 && streq(name, "chosen");
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			if (in_chosen)
				return 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (in_chosen && streq((const char *)(strings + nameoff), prop)) {
				if (len == 4)
					*out = be32(val);
				else if (len == 8)
					*out = (uint64_t)be32(val) << 32 | be32(val + 4);
				else
					return 0;
				return 1;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END or garbage */
			return 0;
		}
	}
}
