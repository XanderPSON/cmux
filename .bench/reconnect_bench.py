#!/usr/bin/env python3
"""Remote open/reconnect bench: two acpmux daemons on one Linux box, the remote behind a private sshd and
a relay that delays each chunk DELAY_MS each way (the probe's DelayRelay).

usage: reconnect_bench.py ACPMUX_BIN FAKE_AGENT LABEL [--opens N] [--redials N] [--drops S:N,S:N]

open:    the local daemon restarted; from its ready line until a prompt sent to the remote chat reads back.
redial:  the tunnel's ssh process ends (the link drops with an EOF); from then until a prompt reads back.
drop S:  iptables drops the relay port for S seconds (needs sudo -n); from the restore until a prompt reads back.
Prints one JSON line per mode with every sample and p50/p95. Kills only the processes it started.
"""
import json, os, queue, shutil, signal, socket, subprocess, sys, tempfile, threading, time

DELAY_S = 0.020
LIMIT_S = 90.0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DelayRelay:
    def __init__(self, listen, target, delay_s):
        self.listen, self.target, self.delay_s = listen, target, delay_s
        self.server = None

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.listen))
        server.listen(16)
        self.server = server
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.server:
            try:
                down, _ = self.server.accept()
            except OSError:
                return
            try:
                up = socket.create_connection(("127.0.0.1", self.target), timeout=10)
                up.settimeout(None)
            except OSError:
                down.close()
                continue
            for a, b in ((down, up), (up, down)):
                self.pipe(a, b)

    def pipe(self, src, dst):
        q = queue.Queue()

        def read():
            while True:
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                q.put((time.monotonic() + self.delay_s, data))
                if not data:
                    return

        def write():
            while True:
                due, data = q.get()
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if not data:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=write, daemon=True).start()


def p(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q * len(v) + 0.5)) - 1))]


class Bench:
    def __init__(self, acpmux, fake, label):
        self.acpmux, self.fake, self.label = acpmux, fake, label
        self.work = tempfile.mkdtemp(prefix="rrbench-")
        self.procs = []
        self.local = None

    def sh(self, argv, env=None, timeout=60, check=True):
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{argv[:4]} exit {r.returncode}: {r.stderr[-400:]} {r.stdout[-400:]}")
        return r

    def env(self, home):
        return {"PATH": f"{self.work}/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home,
                "ACPMUX_HOME": f"{home}/.acpmux", "ACPMUX_SOCKET": f"{home}/s.sock", "ACPMUX_LOGIN_ENV": "0",
                "ACPMUX_SPARE_HARNESS": "0", "TMPDIR": self.work, "USER": os.environ.get("USER", "runner")}

    def setup(self):
        w = self.work
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        if not os.path.exists(sshd):
            self.sh(["sudo", "-n", "apt-get", "install", "-y", "-q", "openssh-server"], timeout=600)
        self.sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", f"{w}/hostkey"])
        self.sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", f"{w}/userkey"])
        shutil.copy(f"{w}/userkey.pub", f"{w}/authorized_keys")
        self.rhome = f"{w}/rhome"
        self.lhome = f"{w}/lhome"
        for d in (f"{self.rhome}/.acpmux", f"{self.lhome}/.acpmux", f"{w}/bin", f"{self.rhome}/work"):
            os.makedirs(d, exist_ok=True)
        os.symlink(self.acpmux, f"{w}/bin/acpmux")
        self.sshd_port, self.relay_port = free_port(), free_port()
        user = self.sh(["id", "-un"]).stdout.strip()
        with open(f"{w}/sshd_config", "w") as f:
            f.write(f"""Port {self.sshd_port}
ListenAddress 127.0.0.1
HostKey {w}/hostkey
AuthorizedKeysFile {w}/authorized_keys
PidFile {w}/sshd.pid
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding yes
ForceCommand HOME={self.rhome}; export HOME; cd "$HOME"; PATH={w}/bin:$PATH; export PATH; exec /bin/sh -c "$SSH_ORIGINAL_COMMAND"
LogLevel ERROR
""")
        self.spawn([sshd, "-D", "-e", "-f", f"{w}/sshd_config"])
        DelayRelay(self.relay_port, self.sshd_port, DELAY_S).start()
        with open(f"{w}/ssh_config", "w") as f:
            f.write(f"""Host box
  HostName 127.0.0.1
  Port {self.relay_port}
  User {user}
  IdentityFile {w}/userkey
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
""")
        with open(f"{w}/bin/ssh", "w") as f:
            f.write(f"#!/bin/sh\nexec /usr/bin/ssh -F {w}/ssh_config \"$@\"\n")
        os.chmod(f"{w}/bin/ssh", 0o755)
        time.sleep(0.5)
        t = time.monotonic()
        self.sh([f"{w}/bin/ssh", "box", "true"], timeout=30)
        self.ssh_login_ms = int((time.monotonic() - t) * 1000)
        # The remote daemon, its stub agent and one chat.
        cfg = {"harnesses": {"fake": {"argv": ["python3", self.fake]}}, "webRoots": [f"{self.rhome}/work"],
               "webAskingModes": {"fake": ["normal", "strict"]}}
        json.dump(cfg, open(f"{self.rhome}/.acpmux/config.json", "w"))
        remote, ready = self.daemon(self.rhome, listen=True)
        self.remote_port = int(ready["listen"].rsplit(":", 1)[1])
        r = self.sh([self.acpmux, "--json", "new", "-d", "-n", "far", "-m", "fake", "--cwd", f"{self.rhome}/work"],
                    env=self.env(self.rhome), timeout=120, check=False)
        if r.returncode != 0:
            r = self.sh([self.acpmux, "--json", "new", "-d", "-n", "far", "-m", "fake"], env=self.env(self.rhome),
                        timeout=120)
        json.dump({"peers": {"box": {"url": f"ssh://box:{self.remote_port}"}}},
                  open(f"{self.lhome}/.acpmux/config.json", "w"))

    def spawn(self, argv, env=None, stdout=subprocess.DEVNULL):
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.procs.append(proc)
        return proc

    def daemon(self, home, listen=False):
        argv = [self.acpmux, "daemon", "run", "--memory", "--ready-fd", "1", "--log", "warn"]
        if listen:
            argv += ["--listen", "127.0.0.1:0"]
        proc = self.spawn(argv, env=self.env(home), stdout=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("daemon exited before ready")
            if line.startswith(b"{"):
                return proc, json.loads(line)
        raise RuntimeError("daemon not ready")

    def stop(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def acp(self, *args, timeout=30):
        return subprocess.run([self.acpmux, *args], env=self.env(self.lhome), capture_output=True, text=True,
                              timeout=timeout)

    def round_trip(self, start):
        token = f"rr-{os.urandom(4).hex()}"
        sent = False
        while time.monotonic() - start < LIMIT_S:
            if not sent:
                sent = self.acp("--json", "send", "box/far", token, timeout=LIMIT_S).returncode == 0
            if sent and token in self.acp("--json", "last", "box/far").stdout:
                return int((time.monotonic() - start) * 1000)
            time.sleep(0.02)
        return None

    def start_local(self):
        self.local, _ = self.daemon(self.lhome)
        return time.monotonic()

    def tunnel_pids(self):
        out = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                stat = open(f"/proc/{pid}/stat").read()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            except (OSError, ValueError, IndexError):
                continue
            if ppid == self.local.pid and b"-W" in cmd:
                out.append(int(pid))
        return out

    def opens(self, n):
        samples = []
        for _ in range(n):
            if self.local:
                self.stop(self.local)
            start = self.start_local()
            samples.append(self.round_trip(start))
        return samples

    def redials(self, n):
        samples = []
        if not self.local:
            self.round_trip(self.start_local())
        for _ in range(n):
            self.round_trip(time.monotonic())  # connected and idle
            time.sleep(1)
            pids = self.tunnel_pids()
            if len(pids) != 1:
                raise RuntimeError(f"tunnel pids {pids}")
            start = time.monotonic()
            os.kill(pids[0], signal.SIGKILL)
            samples.append(self.round_trip(start))
        return samples

    def iptables(self, op):
        for rule in (["--dport", str(self.relay_port)], ["--sport", str(self.relay_port)]):
            self.sh(["sudo", "-n", "iptables", op, "INPUT", "-i", "lo", "-p", "tcp", *rule, "-j", "DROP"])

    def drops(self, seconds, n):
        samples = []
        if not self.local:
            self.round_trip(self.start_local())
        for _ in range(n):
            self.round_trip(time.monotonic())
            time.sleep(1)
            self.iptables("-I")
            try:
                time.sleep(seconds)
            finally:
                self.iptables("-D")
            samples.append(self.round_trip(time.monotonic()))
        return samples

    def close(self):
        for proc in reversed(self.procs):
            if proc.poll() is None:
                self.stop(proc)
        shutil.rmtree(self.work, ignore_errors=True)


def report(label, mode, samples, extra=None):
    ok = [s for s in samples if s is not None]
    row = {"label": label, "mode": mode, "n": len(samples), "failed": len(samples) - len(ok),
           "p50": p(ok, 0.5), "p95": p(ok, 0.95), "max": max(ok) if ok else None, "samples": samples}
    row.update(extra or {})
    print(json.dumps(row), flush=True)


def main(argv):
    acpmux, fake, label = argv[:3]
    opts = dict(zip(argv[3::2], argv[4::2]))
    b = Bench(os.path.abspath(acpmux), os.path.abspath(fake), label)
    try:
        b.setup()
        print(json.dumps({"label": label, "ssh_login_ms": b.ssh_login_ms, "delay_ms_each_way": DELAY_S * 1000}),
              flush=True)
        if int(opts.get("--opens", "0")):
            report(label, "open", b.opens(int(opts["--opens"])))
        if int(opts.get("--redials", "0")):
            report(label, "redial", b.redials(int(opts["--redials"])))
        for spec in filter(None, opts.get("--drops", "").split(",")):
            s, n = (int(x) for x in spec.split(":"))
            report(label, f"drop{s}s", b.drops(s, n))
    finally:
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
