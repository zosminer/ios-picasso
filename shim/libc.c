/*
 * picasso 新增：shim 是裸机程序，没有 C 库。
 * GCC 会把 __builtin_strcmp(变量, "常量") 直接内联，clang 则会生成对 strcmp 的调用，
 * 所以用 clang 构建（shim/build_macos.py）时需要这个最小实现。用 GCC 构建时不会被引用，也不影响结果。
 */
int strcmp(const char *a, const char *b);

int strcmp(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return (unsigned char)*a - (unsigned char)*b;
}
