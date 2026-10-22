/*
 * picasso 新增：在电脑上测试 fdt.c 的新函数（fdt_memory_banks、fdt_node_reg）。
 * 用法：fdt_host_test 设备树.dtb 期望区段数 [期望基址1 期望大小1 ...]
 * 例：fdt_host_test filled.dtb 2 0x80000000 0x80000000 0x100000000 0x180000000
 * 打印读到的内容；与期望不符时退出码为 1。
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "fdt.h"

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "用法：%s 设备树.dtb 期望区段数 [基址 大小 ...]\n", argv[0]);
		return 2;
	}
	FILE *f = fopen(argv[1], "rb");
	if (!f) {
		perror(argv[1]);
		return 2;
	}
	static unsigned char buf[4 << 20] __attribute__((aligned(8)));
	size_t n = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	if (n == 0 || n == sizeof(buf)) {
		fprintf(stderr, "设备树为空或超过 4 MiB\n");
		return 2;
	}
	uint64_t base[8], size[8];
	int banks = fdt_memory_banks((uintptr_t)buf, base, size, 8);
	int fail = banks != atoi(argv[2]);
	printf("DRAM 区段：%d 个\n", banks);
	for (int i = 0; i < banks; i++) {
		printf("  区段 %d：基址 %#llx 大小 %#llx\n", i, (unsigned long long)base[i], (unsigned long long)size[i]);
		if (argc > 4 + 2 * i) {
			uint64_t eb = strtoull(argv[3 + 2 * i], NULL, 0), es = strtoull(argv[4 + 2 * i], NULL, 0);
			if (eb != base[i] || es != size[i])
				fail = 1;
		}
	}
	uint64_t rb, rs;
	if (fdt_node_reg((uintptr_t)buf, "qcom,rmtfs_sharedmem", &rb, &rs))
		printf("RMTFS：基址 %#llx 大小 %#llx\n", (unsigned long long)rb, (unsigned long long)rs);
	else
		printf("RMTFS：未找到\n");
	uint64_t cb = 0;
	if (!fdt_chosen_u64((uintptr_t)buf, "linux,initrd-start", &cb))
		printf("initrd-start：未设置（原厂设备树里本来就没有，ABL 启动时才写入）\n");
	else
		printf("initrd-start：%#llx\n", (unsigned long long)cb);
	printf(fail ? "结果：与期望不符\n" : "结果：符合期望\n");
	return fail;
}
