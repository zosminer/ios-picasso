/*
 * libsweet-agent.dylib：手机上的远程调试代理（picasso 自己写的，不来自别的项目）。
 *
 * 为什么有它：镜像里没有 /bin/sh、ls、cat，每次想看手机里的东西或换一个库都要重刷 userdata。
 * 这个库用 boot-arg sweetinject=/usr/local/lib/libsweet-agent.dylib:configd 插进 configd
 * （root、整个开机过程都在），在 192.168.7.2:2323 上听（内核 sweetusb=2 把 USB 网口 ue0 配成这个地址，
 * 电脑一侧是 192.168.7.1）。只绑 USB 这个地址，不绑所有网口：以后 Wi-Fi 通了也不会暴露。没有认证——
 * 这是开发机，连的是一根 USB 线。
 *
 * 协议：每个请求一行文本（空格分隔，可以用双引号），回复是 "OK <n>\n" 或 "ERR <n>\n" 加 n 字节正文。
 * put 的请求行后面紧跟 <len> 字节内容。命令：
 *   ping | info | ls PATH | stat PATH | cat PATH [OFF [LEN]] | put PATH MODE LEN | rm PATH | mkdir PATH [MODE]
 *   mv FROM TO | chmod MODE PATH | run [-t SECS] PROGRAM [ARGS...] | sysctl NAME | kill SIG PID | reboot
 * 电脑一侧用 tools/sweet-ctl.py。
 */
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define AGENT_PORT      2323
#define BODY_MAX        (16u << 20)     /* 一次回复最多 16 MiB */
#define PUT_MAX         (64u << 20)
#define LINE_MAX_LEN    4096
#define ARGS_MAX        64

extern char **environ;

struct buf {
	char *p;
	size_t n, cap;
};

static void
say(const char *fmt, ...)
{
	char line[256];
	va_list ap;
	int fd, n;

	va_start(ap, fmt);
	n = vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (n <= 0) {
		return;
	}
	fd = open("/dev/console", O_WRONLY | O_NOCTTY);
	if (fd >= 0) {
		(void)!write(fd, line, (size_t)n < sizeof(line) ? (size_t)n : sizeof(line) - 1);
		close(fd);
	}
}

static int
bput(struct buf *b, const void *d, size_t n)
{
	if (b->n + n > BODY_MAX) {
		return -1;
	}
	if (b->n + n + 1 > b->cap) {
		size_t cap = b->cap ? b->cap : 4096;
		char *p;

		while (cap < b->n + n + 1) {
			cap *= 2;
		}
		if ((p = realloc(b->p, cap)) == NULL) {
			return -1;
		}
		b->p = p;
		b->cap = cap;
	}
	memcpy(b->p + b->n, d, n);
	b->n += n;
	b->p[b->n] = 0;
	return 0;
}

static void
bprintf(struct buf *b, const char *fmt, ...)
{
	char line[1024];
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (n > 0) {
		(void)bput(b, line, (size_t)n < sizeof(line) ? (size_t)n : sizeof(line) - 1);
	}
}

static int
write_all(int fd, const void *d, size_t n)
{
	const char *p = d;

	while (n > 0) {
		ssize_t w = write(fd, p, n);

		if (w < 0) {
			if (errno == EINTR) {
				continue;
			}
			return -1;
		}
		p += w;
		n -= (size_t)w;
	}
	return 0;
}

static int
reply(int fd, int ok, const struct buf *b)
{
	char head[48];
	int n = snprintf(head, sizeof(head), "%s %zu\n", ok ? "OK" : "ERR", b->n);

	return write_all(fd, head, (size_t)n) || (b->n && write_all(fd, b->p, b->n)) ? -1 : 0;
}

static int
read_line(int fd, char *line, size_t max)
{
	size_t n = 0;

	for (;;) {
		char c;
		ssize_t r = read(fd, &c, 1);

		if (r < 0 && errno == EINTR) {
			continue;
		}
		if (r <= 0) {
			return -1;
		}
		if (c == '\n') {
			break;
		}
		if (n + 1 < max && c != '\r') {
			line[n++] = c;
		}
	}
	line[n] = 0;
	return 0;
}

/* 空格分隔，双引号里的空格不分；原地改写 */
static int
split(char *line, char **argv, int max)
{
	int argc = 0;
	char *c = line;

	while (*c != 0 && argc < max - 1) {
		while (*c == ' ' || *c == '\t') {
			c++;
		}
		if (*c == 0) {
			break;
		}
		if (*c == '"') {
			argv[argc++] = ++c;
			while (*c != 0 && *c != '"') {
				c++;
			}
		} else {
			argv[argc++] = c;
			while (*c != 0 && *c != ' ' && *c != '\t') {
				c++;
			}
		}
		if (*c != 0) {
			*c++ = 0;
		}
	}
	argv[argc] = NULL;
	return argc;
}

static void
fail(struct buf *out, const char *what)
{
	bprintf(out, "%s: %s", what, strerror(errno));
}

static char
type_char(mode_t m)
{
	switch (m & S_IFMT) {
	case S_IFDIR: return 'd';
	case S_IFLNK: return 'l';
	case S_IFREG: return '-';
	case S_IFCHR: return 'c';
	case S_IFBLK: return 'b';
	case S_IFSOCK: return 's';
	default: return '?';
	}
}

static int
cmd_ls(struct buf *out, const char *path)
{
	DIR *d = opendir(path);
	struct dirent *e;

	if (d == NULL) {
		fail(out, path);
		return 0;
	}
	while ((e = readdir(d)) != NULL) {
		char full[1024];
		struct stat st;

		snprintf(full, sizeof(full), "%s/%s", path, e->d_name);
		if (lstat(full, &st) == 0) {
			bprintf(out, "%c %04o %lld %s\n", type_char(st.st_mode), st.st_mode & 07777, (long long)st.st_size, e->d_name);
		} else {
			bprintf(out, "? 0000 0 %s\n", e->d_name);
		}
	}
	closedir(d);
	return 1;
}

static int
cmd_stat(struct buf *out, const char *path)
{
	struct stat st;

	if (lstat(path, &st) != 0) {
		fail(out, path);
		return 0;
	}
	bprintf(out, "%c %04o uid %u gid %u size %lld mtime %lld\n", type_char(st.st_mode), st.st_mode & 07777, st.st_uid,
	    st.st_gid, (long long)st.st_size, (long long)st.st_mtime);
	return 1;
}

static int
cmd_cat(struct buf *out, const char *path, long long off, long long len)
{
	int fd = open(path, O_RDONLY);
	char chunk[16384];

	if (fd < 0) {
		fail(out, path);
		return 0;
	}
	if (off > 0 && lseek(fd, (off_t)off, SEEK_SET) < 0) {
		fail(out, "seek");
		close(fd);
		return 0;
	}
	while (len != 0) {
		size_t want = sizeof(chunk);
		ssize_t r;

		if (len > 0 && (long long)want > len) {
			want = (size_t)len;
		}
		r = read(fd, chunk, want);
		if (r < 0 && errno == EINTR) {
			continue;
		}
		if (r <= 0) {
			break;
		}
		if (bput(out, chunk, (size_t)r) != 0) {
			break;
		}
		if (len > 0) {
			len -= r;
		}
	}
	close(fd);
	return 1;
}

static int
cmd_put(int sock, struct buf *out, const char *path, mode_t mode, long long len)
{
	int fd;
	char chunk[16384];
	int ok = 1;

	if (len < 0 || len > (long long)PUT_MAX) {
		bprintf(out, "put: length %lld not allowed", len);
		return 0;
	}
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
	if (fd < 0) {
		fail(out, path);
		ok = 0;
	}
	while (len > 0) {            /* 出错也要把内容读完，不然协议错位 */
		size_t want = (size_t)len < sizeof(chunk) ? (size_t)len : sizeof(chunk);
		ssize_t r = read(sock, chunk, want);

		if (r < 0 && errno == EINTR) {
			continue;
		}
		if (r <= 0) {
			ok = 0;
			break;
		}
		if (fd >= 0 && write_all(fd, chunk, (size_t)r) != 0) {
			fail(out, "write");
			close(fd);
			fd = -1;
			ok = 0;
		}
		len -= r;
	}
	if (fd >= 0) {
		fchmod(fd, mode);
		close(fd);
	}
	if (ok) {
		bprintf(out, "written");
	}
	return ok;
}

static int
cmd_run(struct buf *out, char **argv, int timeout)
{
	int pipefd[2];
	posix_spawn_file_actions_t fa;
	pid_t pid;
	int rc, status = 0;
	time_t end = time(NULL) + timeout;
	int timed_out = 0;

	if (pipe(pipefd) != 0) {
		fail(out, "pipe");
		return 0;
	}
	posix_spawn_file_actions_init(&fa);
	posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
	posix_spawn_file_actions_adddup2(&fa, pipefd[1], 1);
	posix_spawn_file_actions_adddup2(&fa, pipefd[1], 2);
	posix_spawn_file_actions_addclose(&fa, pipefd[0]);
	posix_spawn_file_actions_addclose(&fa, pipefd[1]);
	rc = posix_spawn(&pid, argv[0], &fa, NULL, argv, environ);
	posix_spawn_file_actions_destroy(&fa);
	close(pipefd[1]);
	if (rc != 0) {
		close(pipefd[0]);
		bprintf(out, "%s: %s", argv[0], strerror(rc));
		return 0;
	}
	for (;;) {
		struct pollfd p = { .fd = pipefd[0], .events = POLLIN };
		char chunk[8192];
		ssize_t r;
		int left = (int)(end - time(NULL));

		if (left <= 0) {
			timed_out = 1;
			kill(pid, SIGKILL);
			break;
		}
		if (poll(&p, 1, left * 1000) <= 0) {
			continue;
		}
		r = read(pipefd[0], chunk, sizeof(chunk));
		if (r <= 0) {
			break;
		}
		if (bput(out, chunk, (size_t)r) != 0) {
			kill(pid, SIGKILL);
			break;
		}
	}
	close(pipefd[0]);
	while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
	}
	if (timed_out) {
		bprintf(out, "\n[timeout after %d s: killed]\n", timeout);
	} else if (WIFEXITED(status)) {
		bprintf(out, "\n[exit %d]\n", WEXITSTATUS(status));
	} else if (WIFSIGNALED(status)) {
		bprintf(out, "\n[signal %d]\n", WTERMSIG(status));
	}
	return 1;
}

static int
cmd_sysctl(struct buf *out, const char *name)
{
	size_t size = 0;
	char *value;

	if (sysctlbyname(name, NULL, &size, NULL, 0) != 0) {
		fail(out, name);
		return 0;
	}
	if ((value = calloc(1, size + 8)) == NULL) {
		return 0;
	}
	if (sysctlbyname(name, value, &size, NULL, 0) != 0) {
		fail(out, name);
		free(value);
		return 0;
	}
	if (size == 4) {
		bprintf(out, "%d\n", *(int32_t *)value);
	} else if (size == 8) {
		bprintf(out, "%lld\n", (long long)*(int64_t *)value);
	} else {
		bput(out, value, size);
		bprintf(out, "\n");
	}
	free(value);
	return 1;
}

static void
cmd_info(struct buf *out)
{
	static const char *names[] = { "kern.version", "kern.osversion", "hw.machine", "hw.model", "hw.ncpu", "kern.boottime",
		                       "kern.bootargs" };

	bprintf(out, "pid %d uid %d\n", getpid(), getuid());
	for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
		struct buf one = { 0 };

		if (cmd_sysctl(&one, names[i])) {
			bprintf(out, "%s: %s", names[i], one.p ? one.p : "\n");
		}
		free(one.p);
	}
}

static int
dispatch(int sock, char *line)
{
	char *argv[ARGS_MAX];
	struct buf out = { 0 };
	int argc = split(line, argv, ARGS_MAX), ok = 1, rc;

	if (argc == 0) {
		bprintf(&out, "empty");
		rc = reply(sock, 0, &out);
	} else {
		const char *c = argv[0];

		if (!strcmp(c, "ping")) {
			bprintf(&out, "pong");
		} else if (!strcmp(c, "info")) {
			cmd_info(&out);
		} else if (!strcmp(c, "ls") && argc >= 2) {
			ok = cmd_ls(&out, argv[1]);
		} else if (!strcmp(c, "stat") && argc >= 2) {
			ok = cmd_stat(&out, argv[1]);
		} else if (!strcmp(c, "cat") && argc >= 2) {
			ok = cmd_cat(&out, argv[1], argc > 2 ? atoll(argv[2]) : 0, argc > 3 ? atoll(argv[3]) : -1);
		} else if (!strcmp(c, "put") && argc >= 4) {
			ok = cmd_put(sock, &out, argv[1], (mode_t)strtol(argv[2], NULL, 8), atoll(argv[3]));
		} else if (!strcmp(c, "rm") && argc >= 2) {
			ok = (unlink(argv[1]) == 0 || rmdir(argv[1]) == 0);
			if (!ok) {
				fail(&out, argv[1]);
			}
		} else if (!strcmp(c, "mkdir") && argc >= 2) {
			ok = mkdir(argv[1], argc > 2 ? (mode_t)strtol(argv[2], NULL, 8) : 0755) == 0;
			if (!ok) {
				fail(&out, argv[1]);
			}
		} else if (!strcmp(c, "mv") && argc >= 3) {
			ok = rename(argv[1], argv[2]) == 0;
			if (!ok) {
				fail(&out, "rename");
			}
		} else if (!strcmp(c, "chmod") && argc >= 3) {
			ok = chmod(argv[2], (mode_t)strtol(argv[1], NULL, 8)) == 0;
			if (!ok) {
				fail(&out, argv[2]);
			}
		} else if (!strcmp(c, "run") && argc >= 2) {
			int timeout = 30, first = 1;

			if (!strcmp(argv[1], "-t") && argc >= 4) {
				timeout = atoi(argv[2]);
				first = 3;
			}
			ok = cmd_run(&out, argv + first, timeout > 0 ? timeout : 30);
		} else if (!strcmp(c, "sysctl") && argc >= 2) {
			ok = cmd_sysctl(&out, argv[1]);
		} else if (!strcmp(c, "kill") && argc >= 3) {
			ok = kill((pid_t)atoi(argv[2]), atoi(argv[1])) == 0;
			if (!ok) {
				fail(&out, "kill");
			}
		} else if (!strcmp(c, "reboot")) {
			bprintf(&out, "rebooting");
			reply(sock, 1, &out);
			free(out.p);
			sync();
			sleep(1);
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
			syscall(SYS_reboot, 0, NULL);
#pragma clang diagnostic pop
			return -1;
		} else {
			bprintf(&out, "unknown or incomplete command: %s", c);
			ok = 0;
		}
		rc = reply(sock, ok, &out);
	}
	free(out.p);
	return rc;
}

static void *
serve(void *arg)
{
	int sock = (int)(intptr_t)arg;
	char line[LINE_MAX_LEN];

	while (read_line(sock, line, sizeof(line)) == 0) {
		if (dispatch(sock, line) != 0) {
			break;
		}
	}
	close(sock);
	return NULL;
}

static void *
listener(void *unused)
{
	(void)unused;
	for (;;) {
		struct sockaddr_in addr = { 0 };
		int one = 1, s = socket(AF_INET, SOCK_STREAM, 0);

		if (s < 0) {
			sleep(2);
			continue;
		}
		setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
		addr.sin_family = AF_INET;
		addr.sin_port = htons(AGENT_PORT);
		addr.sin_addr.s_addr = htonl(0xc0a80702);       /* 192.168.7.2：USB 网口，网口还没起来时 bind 失败，重试 */
		if (bind(s, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(s, 4) != 0) {
			close(s);
			sleep(1);
			continue;
		}
		say("sweet-agent: listening on 192.168.7.2:%d (pid %d)\n", AGENT_PORT, getpid());
		for (;;) {
			int c = accept(s, NULL, NULL);
			pthread_t t;
			pthread_attr_t attr;

			if (c < 0) {
				if (errno == EINTR) {
					continue;
				}
				break;
			}
			pthread_attr_init(&attr);
			pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
			if (pthread_create(&t, &attr, serve, (void *)(intptr_t)c) != 0) {
				close(c);
			}
			pthread_attr_destroy(&attr);
		}
		close(s);
	}
	return NULL;
}

__attribute__((constructor)) static void
agent_start(void)
{
	pthread_t t;
	pthread_attr_t attr;

	/* 子进程（run 起来的程序）不要再带着这个库，否则每个都想听同一个端口 */
	unsetenv("DYLD_INSERT_LIBRARIES");
	signal(SIGPIPE, SIG_IGN);
	pthread_attr_init(&attr);
	pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
	if (pthread_create(&t, &attr, listener, NULL) != 0) {
		say("sweet-agent: no thread\n");
	}
	pthread_attr_destroy(&attr);
}
