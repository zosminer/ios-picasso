/*
 * Milestone 4: act as iBoot for xnu.
 *
 * mkpayload.py already resolved the kernel's chained fixups and built the
 * ADT, so this only has to: find the payload in the ABL ramdisk, copy the
 * segments to phys_base + (vmaddr - virt_base), place the ADT and
 * boot_args behind the kernel, and jump to the entry point with the MMU
 * and caches off, x0 = boot_args (physical).
 */
#include <stdint.h>
#include "console.h"
#include "fdt.h"
#include "loader.h"

#define BOOT_LINE_LENGTH	608
#define PAGE_16K		0x4000UL

struct payload_seg {
	uint64_t vmaddr, vmsize, file_off, file_size;
};

struct payload {
	char magic[8];
	uint32_t version, nsegs;
	uint64_t virt_base, entry_va, adt_off, adt_size, kernel_end;
	uint64_t phys_base, mem_size;
	char cmdline[BOOT_LINE_LENGTH];
	struct payload_seg segs[];
};

/* pexpert/pexpert/arm64/boot.h, Revision/Version 2 */
struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t Revision, Version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video Video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char CommandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags, memSizeActual;
};

_Static_assert(__builtin_offsetof(struct payload, segs) == 0x2a8, "payload header layout");
_Static_assert(__builtin_offsetof(struct boot_args, Video) == 40, "boot_args layout");
_Static_assert(__builtin_offsetof(struct boot_args, CommandLine) == 108, "boot_args layout");
_Static_assert(sizeof(struct boot_args) == 736, "boot_args layout");

/* MMU off: everything is Device memory, so copy in aligned 8-byte words. */
static void copy64(uintptr_t dst, uintptr_t src, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	volatile const uint64_t *s = (volatile const uint64_t *)src;
	for (uint64_t i = 0; i < (n + 7) / 8; i++)
		d[i] = s[i];
}

static void zero64(uintptr_t dst, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	for (uint64_t i = 0; i < n / 8; i++)
		d[i] = 0;
}

static int magic_ok(const char *m)
{
	static const char want[] = "SWEETXNU";
	for (int i = 0; i < 8; i++)
		if (m[i] != want[i])
			return 0;
	return 1;
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);

/*
 * picasso 新增：ABL 没给设备树时，在它的堆里找负载。
 *
 * 依据另一台 K30 5G 的真机结果（github.com/minhtritt1996/ios-picasso NOTES.md，2026-10-21）：
 * - ABL 进入 shim 时 x0 = x1 = x2 = 0xA7EB0000 左右（每次开机略有变化），不是设备树，
 *   而是 ABL 堆里的地址；静态设备树里也没有 linux,initrd-*。
 * - boot.img 头里写的 ramdisk 地址（以及内核部分后面的内容）启动时已经被 ABL 释放，
 *   读这些地址会触发 TrustZone 安全异常，CPU 当场停住，屏幕上没有任何报错。
 * - ABL 读进来的整个 boot.img 还留在 x0 附近的堆里：负载在 0xA7EC0000，
 *   离 x0 只有十几页。
 * 所以从 x0 开始，向上、向下交替一页一页地看每页开头 8 字节是不是 "SWEETXNU"，
 * 找到就立刻停：越过负载继续往外走会碰到没映射的页（对方真机上因此反复重启过）。
 */
#define HUNT_PAGE	0x1000UL
#ifndef HUNT_WINDOW
#define HUNT_WINDOW	0x4000000UL	/* 每个方向最多 64 MiB */
#endif
#ifndef DDR_LO
#define DDR_LO		0x80000000UL	/* SM7250 的内存从这里开始，更低的地址是 XBL/AOP/TZ 等受保护区域 */
#endif
#ifndef DDR_HI
#define DDR_HI		0x200000000UL	/* 8 GiB 机型的内存上限 */
#endif

static int page_is_payload(uintptr_t a)
{
	volatile const uint32_t *w = (volatile const uint32_t *)a;
	return w[0] == 0x45455753U && w[1] == 0x554e5854U;	/* "SWEE" "TXNU"（小端） */
}

static uintptr_t hunt_payload(uintptr_t x0)
{
	if (x0 < DDR_LO || x0 >= DDR_HI)
		return 0;
	uintptr_t base = x0 & ~(HUNT_PAGE - 1);
	con_hex("HUNT from", base);
	for (uintptr_t off = 0; off < HUNT_WINDOW; off += HUNT_PAGE) {
		uintptr_t up = base + off;
		if (up < DDR_HI && page_is_payload(up))
			return up;
		if (off && base - off >= DDR_LO && page_is_payload(base - off))
			return base - off;
	}
	return 0;
}

/* 负载的实际长度：段数据和 ADT 中最靠后的结尾 */
static uint64_t payload_size(const struct payload *p)
{
	uint64_t end = p->adt_off + p->adt_size;
	for (uint32_t i = 0; i < p->nsegs; i++) {
		uint64_t e = p->segs[i].file_off + p->segs[i].file_size;
		if (e > end)
			end = e;
	}
	return end;
}

const char *loader_boot(uintptr_t dtb)
{
	uint64_t rd_start, rd_end;

	if (fdt_chosen_u64(dtb, "linux,initrd-start", &rd_start) &&
	    fdt_chosen_u64(dtb, "linux,initrd-end", &rd_end)) {
		con_str("INITRD", "from /chosen");
	} else {
		/* picasso：x0 不是设备树（或里面没有 initrd），去 ABL 的堆里找 */
		con_str("INITRD", "not in a device tree, hunting near x0");
		rd_start = hunt_payload(dtb);
		if (!rd_start)
			return "no SWEETXNU payload near x0";
		con_hex("HUNT found", rd_start);
		const struct payload *hp = (const struct payload *)rd_start;
		if (hp->version != 1 || hp->nsegs > 64)
			return "bad payload header";
		rd_end = rd_start + payload_size(hp);
	}
	con_hex2("INITRD", "lo=", rd_start, "hi=", rd_end);

	const struct payload *p = (const struct payload *)rd_start;
	if (!magic_ok(p->magic))
		return "no SWEETXNU payload";
	if (p->version != 1 || (p->adt_off & 7) || (rd_start & 7))
		return "bad payload header";

	uint64_t pa_of_va_base = p->phys_base - p->virt_base;	/* pa = va + this */
	con_hex2("PHYS", "base=", p->phys_base, "size=", p->mem_size);
	con_hex("VIRT", p->virt_base);
	con_hex("ENTRY", p->entry_va);

	/* The payload must not overlap where the kernel goes. */
	if (rd_end > p->phys_base && rd_start < p->phys_base + p->mem_size)
		return "ramdisk overlaps kernel memory";

	for (uint32_t i = 0; i < p->nsegs; i++) {
		const struct payload_seg *s = &p->segs[i];
		uintptr_t dst = s->vmaddr + pa_of_va_base;
		copy64(dst, rd_start + s->file_off, s->file_size);
		if (s->vmsize > s->file_size)
			zero64(dst + ((s->file_size + 7) & ~7UL), s->vmsize - ((s->file_size + 7) & ~7UL));
	}
	con_hex2("SEGS", "n=", p->nsegs, "end=", p->kernel_end + pa_of_va_base);

	/* ADT, then boot_args, behind the kernel; everything below topOfKernelData is ours. */
	uintptr_t adt_pa = p->kernel_end + pa_of_va_base;
	copy64(adt_pa, rd_start + p->adt_off, p->adt_size);
	uintptr_t ba_pa = (adt_pa + p->adt_size + PAGE_16K - 1) & ~(PAGE_16K - 1);
	uintptr_t top = ba_pa + PAGE_16K;

	zero64(ba_pa, PAGE_16K);
	volatile struct boot_args *ba = (volatile struct boot_args *)ba_pa;
	ba->Revision = 2;
	ba->Version = 2;
	ba->virtBase = p->virt_base;
	ba->physBase = p->phys_base;
	ba->memSize = p->mem_size;
	ba->topOfKernelData = top;
	ba->Video.v_baseAddr = FB_BASE;
	ba->Video.v_display = 1;
	ba->Video.v_rowBytes = FB_STRIDE;
	ba->Video.v_width = FB_W;
	ba->Video.v_height = FB_H;
	ba->Video.v_depth = 32;
	ba->deviceTreeP = adt_pa - pa_of_va_base;	/* xnu reads it with the MMU on: a VA */
	ba->deviceTreeLength = (uint32_t)p->adt_size;
	for (int i = 0; i < BOOT_LINE_LENGTH; i++)
		ba->CommandLine[i] = p->cmdline[i];
	/*
	 * picasso 新增：把 ABL 选中的面板编号追加成 boot-arg picassopanel=37_02_0a，
	 * 内核触控驱动（bsd/dev/picasso_touch.c）按它从 ADT 选 Novatek 固件。
	 */
	{
		char panel[16];
		if (fdt_chosen_panel(dtb, panel, sizeof(panel))) {
			static const char key[] = " picassopanel=";
			int n = 0, need = 0;
			while (n < BOOT_LINE_LENGTH - 1 && ba->CommandLine[n])
				n++;
			while (panel[need])
				need++;
			if (n + (int)sizeof(key) - 1 + need < BOOT_LINE_LENGTH) {
				for (int i = 0; key[i]; i++)
					ba->CommandLine[n++] = key[i];
				for (int i = 0; panel[i]; i++)
					ba->CommandLine[n++] = panel[i];
				ba->CommandLine[n] = 0;
				con_str("PANEL", panel);
			} else {
				con_str("PANEL", "cmdline full, picassopanel not added");
			}
		} else {
			con_str("PANEL", "not found in ABL bootargs");
		}
	}
	ba->memSizeActual = p->mem_size;
	con_hex2("BOOTARG", "pa=", ba_pa, "top=", top);

	uintptr_t entry_pa = p->entry_va + pa_of_va_base;
	con_hex("JUMP", entry_pa);
	con_str("XNU", "jumping (fbcon clears screen)");

	/* Everything was written with the MMU and caches off; drop any stale I-cache lines too. */
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
	((xnu_entry_t)entry_pa)(ba_pa);
	return "xnu returned";
}
