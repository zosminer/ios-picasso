#!/usr/bin/env python3
"""电脑一侧控制手机上的 libsweet-agent（userland/agent/sweet_agent.c）。

  tools/sweet-ctl.py [-H 192.168.7.2] [-p 2323] 命令 参数...

命令：
  ping | info
  ls PATH | stat PATH | cat PATH [OFF [LEN]] | rm PATH | mkdir PATH | mv A B | chmod MODE PATH
  get PATH LOCAL        手机文件拷到电脑
  put LOCAL PATH [MODE] 电脑文件放到手机（默认 0755）
  run [-t SECS] PROGRAM [ARGS...]   在手机上运行程序，回显输出（手机上没有 sh：直接给程序路径）
  sysctl NAME | kill SIG PID | reboot
  wait                  等代理上线（最多 300 秒）

前提：手机用 sweetusb=2 启动；电脑一侧给手机的 USB 网口地址 192.168.7.1/24
（macOS：ifconfig 里找新出现的 enN，sudo ifconfig enN 192.168.7.1/24 up）。
"""
import os
import socket
import sys
import time


class Agent:
    def __init__(self, host='192.168.7.2', port=2323, timeout=60):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.f = self.s.makefile('rwb')

    def call(self, line, payload=None):
        self.f.write(line.encode() + b'\n')
        if payload is not None:
            self.f.write(payload)
        self.f.flush()
        head = self.f.readline().decode().split()
        if len(head) != 2:
            raise RuntimeError('协议错误：' + repr(head))
        body = self.f.read(int(head[1]))
        return head[0] == 'OK', body


def quote(a):
    return '"' + a + '"' if (' ' in a or not a) else a


def main(argv):
    host, port = '192.168.7.2', 2323
    while argv and argv[0] in ('-H', '-p'):
        if argv[0] == '-H':
            host = argv[1]
        else:
            port = int(argv[1])
        argv = argv[2:]
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == 'wait':
        end = time.time() + 300
        while time.time() < end:
            try:
                ok, body = Agent(host, port, 5).call('ping')
                print(body.decode())
                return 0
            except OSError:
                time.sleep(2)
        print('代理没有上线', file=sys.stderr)
        return 1
    a = Agent(host, port)
    if cmd == 'get':
        ok, body = a.call('cat ' + quote(args[0]))
        if ok:
            open(args[1], 'wb').write(body)
            print(f'{args[0]} -> {args[1]} ({len(body)} bytes)')
            return 0
    elif cmd == 'put':
        data = open(args[0], 'rb').read()
        mode = args[2] if len(args) > 2 else '755'
        ok, body = a.call(f'put {quote(args[1])} {mode} {len(data)}', data)
    else:
        ok, body = a.call(' '.join([cmd] + [quote(x) for x in args]))
    sys.stdout.buffer.write(body)
    if body and not body.endswith(b'\n'):
        print()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
