/*
 * picasso 新增：在电脑上测试 loader.c 的“在 ABL 堆里找负载”（hunt_payload）和负载长度计算。
 * 把 loader.c 整个包含进来，用一块普通内存模拟 ABL 的堆；DDR 上下限放开到整个地址空间，
 * 搜索范围缩小到 1 MiB，免得越出这块内存。
 * 用法（shim/build_macos.py 会自动编译运行）：loader_host_test；全部通过时退出码为 0。
 */
#define DDR_LO		0x1000UL
#define DDR_HI		(~0UL)
#define HUNT_WINDOW	0x100000UL
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "loader.c"

static int failures;
#define CHECK(c) do { if (!(c)) { printf("失败 %s:%d: %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

int main(void)
{
	static uint32_t fb[FB_H * FB_STRIDE / 4];
	con_fb = fb;
	/* 4 MiB 的“堆”，x0 指在中间 */
	const size_t heap_size = 4 << 20;
	uint8_t *heap = aligned_alloc(0x1000, heap_size);
	memset(heap, 0xa5, heap_size);
	uintptr_t x0 = (uintptr_t)heap + (2 << 20);

	/* 1. 负载在 x0 上方 15 页（对方真机：x0 0xA7EB1000，负载 0xA7EC0000） */
	uint8_t *pl = (uint8_t *)(x0 + 15 * 0x1000);
	memcpy(pl, "SWEETXNU", 8);
	CHECK(hunt_payload(x0) == (uintptr_t)pl);
	/* x0 不在页首也能找到 */
	CHECK(hunt_payload(x0 + 0x123) == (uintptr_t)pl);

	/* 2. 负载在 x0 下方：上下两边都有时取先走到的那一边（距离近的） */
	uint8_t *below = (uint8_t *)(x0 - 3 * 0x1000);
	memcpy(below, "SWEETXNU", 8);
	CHECK(hunt_payload(x0) == (uintptr_t)below);
	memset(below, 0xa5, 8);

	/* 3. 只有前 4 字节对上不算 */
	memset(pl, 0xa5, 8);
	memcpy(pl, "SWEEXXXX", 8);
	CHECK(hunt_payload(x0) == 0);

	/* 4. 不在页首的魔数不算（负载总是从页首开始） */
	memcpy(pl + 8, "SWEETXNU", 8);
	CHECK(hunt_payload(x0) == 0);

	/* 5. x0 为 0 或低于 DDR 下限：不搜 */
	CHECK(hunt_payload(0) == 0);

	/* 6. 负载长度 = 段数据和 ADT 里最靠后的结尾 */
	struct payload *p = calloc(1, sizeof(struct payload) + 3 * sizeof(struct payload_seg));
	p->nsegs = 3;
	p->adt_off = 0x500000;
	p->adt_size = 0x3000;
	p->segs[0].file_off = 0x4000;   p->segs[0].file_size = 0x100000;
	p->segs[1].file_off = 0x104000; p->segs[1].file_size = 0x3fc000;
	p->segs[2].file_off = 0x600000; p->segs[2].file_size = 0;
	CHECK(payload_size(p) == 0x600000);
	p->segs[2].file_size = 0x10;
	CHECK(payload_size(p) == 0x600010);
	p->nsegs = 1;
	CHECK(payload_size(p) == 0x503000);

	printf(failures ? "负载搜索自测：%d 项失败\n" : "负载搜索自测：全部通过\n", failures);
	free(p);
	free(heap);
	return failures != 0;
}
