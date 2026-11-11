/*
 * lka-shim: the last step between the measurement helper and a checker.
 *
 *   perf stat -D -1 --control fifo:CTL,ACK ... -- \
 *     lka-shim --cgroup DIR --uid U --gid G --workdir DIR --env FILE --tmp-bytes N \
 *              --overlay HOME --input FILE --input-gid G --hide DIR \
 *              --ctl CTL --ack ACK --status FILE -- COMMAND
 *
 * As root it joins the run's cgroup, turns core dumps off, gives the run its own network
 * namespace (loopback only) and mount namespace (a private /tmp and /dev/shm, and the checker's
 * home behind an overlay whose writes live in that /tmp and vanish with the run, so no run can
 * leave anything for the next). In that namespace the store of staged inputs (--hide) is covered
 * by an empty read-only tmpfs and the run's own input alone shown at its own path, read-only: the
 * store is readable only by the input group, which the run is given and a build never is, so no
 * checker ever sees a round's other inputs or its invalid twins. Then it drops to the checker's
 * own user for good. Only then does
 * it switch perf's counters on, so its own start-up is never measured. It clocks the checker
 * from just before the exec to the moment `waitpid` returns, switches the counters off, and
 * writes the checker's exit status, its wall time and the largest resident set of any process
 * it waited for as one JSON object.
 *
 * perf itself reports exit status 0 for a child killed by a signal, which is why the shim stays
 * the checker's parent instead of exec'ing it.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <net/if.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_ENV 256
#define MAX_ENV_FILE (64 * 1024)

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("lka-shim: ", stderr);
    vfprintf(stderr, fmt, ap);
    if (errno) fprintf(stderr, ": %s", strerror(errno));
    fputc('\n', stderr);
    va_end(ap);
    _exit(125);
}

static void write_file(const char *path, const char *text) {
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0) die("open %s", path);
    size_t len = strlen(text);
    if (write(fd, text, len) != (ssize_t)len) die("write %s", path);
    close(fd);
}

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* Send a command over perf's control FIFO and wait for its acknowledgement. */
static void perf_control(int ctl, int ack, const char *command) {
    size_t len = strlen(command);
    if (write(ctl, command, len) != (ssize_t)len) die("perf control %s", command);
    char buf[16];
    ssize_t n = read(ack, buf, sizeof buf - 1);
    if (n <= 0) die("perf did not acknowledge %s", command);
    buf[n] = '\0';
    if (strncmp(buf, "ack", 3) != 0) die("perf answered '%s' to %s", buf, command);
}

static void loopback_up(void) {
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0) die("socket");
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    strncpy(ifr.ifr_name, "lo", IFNAMSIZ - 1);
    if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0) die("SIOCGIFFLAGS lo");
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0) die("SIOCSIFFLAGS lo");
    close(s);
}

static void make_dir(const char *path, mode_t mode) {
    if (mkdir(path, mode) < 0 && errno != EEXIST) die("mkdir %s", path);
}

/* The checker's home, writable for the run, its changes kept only in the run's private /tmp. */
static void throwaway_overlay(const char *lower) {
    make_dir("/tmp/.lka-overlay", 0700);
    make_dir("/tmp/.lka-overlay/upper", 0755);
    make_dir("/tmp/.lka-overlay/work", 0700);
    struct stat st;
    if (stat(lower, &st) < 0) die("stat %s", lower);
    if (chown("/tmp/.lka-overlay/upper", st.st_uid, st.st_gid) < 0) die("chown overlay upper");
    if (chmod("/tmp/.lka-overlay/upper", st.st_mode & 07777) < 0) die("chmod overlay upper");
    char options[8192];
    int n = snprintf(options, sizeof options, "lowerdir=%s,upperdir=/tmp/.lka-overlay/upper,workdir=/tmp/.lka-overlay/work", lower);
    if (n < 0 || (size_t)n >= sizeof options) { errno = 0; die("overlay path too long"); }
    if (mount("overlay", lower, "overlay", 0, options) < 0) die("mount overlay on %s", lower);
}

/* The store under `hide` covered by an empty read-only tmpfs, with `input` alone back at its own
 * path, bound read-only from a descriptor taken before the store was covered. */
static void isolate_input(const char *input, const char *hide) {
    size_t n = strlen(hide);
    if (strncmp(input, hide, n) != 0 || input[n] != '/') { errno = 0; die("%s is not inside %s", input, hide); }
    char path[4096];
    if (snprintf(path, sizeof path, "%s", input) >= (int)sizeof path) { errno = 0; die("input path too long"); }
    int fd = open(input, O_PATH | O_CLOEXEC);
    if (fd < 0) die("open %s", input);
    if (mount("tmpfs", hide, "tmpfs", MS_NOSUID | MS_NODEV, "size=1m,mode=0755") < 0) die("mount tmpfs on %s", hide);
    for (char *p = path + n + 1; (p = strchr(p, '/')) != NULL; p++) {
        *p = '\0';
        make_dir(path, 0755);
        *p = '/';
    }
    int made = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0444);
    if (made < 0) die("create %s", path);
    close(made);
    char source[64];
    snprintf(source, sizeof source, "/proc/self/fd/%d", fd);
    if (mount(source, path, NULL, MS_BIND, NULL) < 0) die("bind %s", input);
    if (mount(NULL, path, NULL, MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV, NULL) < 0)
        die("make %s read-only", input);
    close(fd);
    if (mount(NULL, hide, NULL, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, NULL) < 0) die("make %s read-only", hide);
}

static void private_tmpfs(const char *where, unsigned long long bytes) {
    char options[64];
    snprintf(options, sizeof options, "size=%llu,mode=1777", bytes);
    if (mount("tmpfs", where, "tmpfs", MS_NOSUID | MS_NODEV, options) < 0) die("mount tmpfs on %s", where);
}

/* KEY=VALUE lines; nothing from the helper's or perf's environment reaches the checker. */
static char **read_env(const char *path) {
    static char text[MAX_ENV_FILE + 1];
    static char *envp[MAX_ENV + 1];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) die("open %s", path);
    ssize_t n = read(fd, text, MAX_ENV_FILE);
    if (n < 0) die("read %s", path);
    close(fd);
    text[n] = '\0';
    int count = 0;
    for (char *line = strtok(text, "\n"); line; line = strtok(NULL, "\n")) {
        if (!strchr(line, '=')) continue;
        if (count == MAX_ENV) { errno = 0; die("more than %d environment entries", MAX_ENV); }
        envp[count++] = line;
    }
    envp[count] = NULL;
    return envp;
}

static const char *arg_value(int *i, int argc, char **argv) {
    if (*i + 1 >= argc) { errno = 0; die("%s needs a value", argv[*i]); }
    return argv[++*i];
}

int main(int argc, char **argv) {
    const char *cgroup = NULL, *workdir = NULL, *env_file = NULL, *ctl_path = NULL, *ack_path = NULL,
               *status_path = NULL, *overlay = NULL, *input = NULL, *hide = NULL;
    long uid = -1, gid = -1, input_gid = -1;
    unsigned long long tmp_bytes = 0;
    int command = -1;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--")) { command = i + 1; break; }
        else if (!strcmp(argv[i], "--cgroup")) cgroup = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--uid")) uid = strtol(arg_value(&i, argc, argv), NULL, 10);
        else if (!strcmp(argv[i], "--gid")) gid = strtol(arg_value(&i, argc, argv), NULL, 10);
        else if (!strcmp(argv[i], "--workdir")) workdir = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--env")) env_file = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--tmp-bytes")) tmp_bytes = strtoull(arg_value(&i, argc, argv), NULL, 10);
        else if (!strcmp(argv[i], "--overlay")) overlay = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--input")) input = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--input-gid")) input_gid = strtol(arg_value(&i, argc, argv), NULL, 10);
        else if (!strcmp(argv[i], "--hide")) hide = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--ctl")) ctl_path = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--ack")) ack_path = arg_value(&i, argc, argv);
        else if (!strcmp(argv[i], "--status")) status_path = arg_value(&i, argc, argv);
        else { errno = 0; die("unknown argument %s", argv[i]); }
    }
    errno = 0;
    if (command < 0 || command >= argc) die("no command after --");
    if (!cgroup || !workdir || !env_file || !ctl_path || !ack_path || !status_path || uid <= 0 || gid <= 0 || !tmp_bytes)
        die("missing --cgroup, --uid, --gid, --workdir, --env, --tmp-bytes, --ctl, --ack or --status");
    if (!input || !hide || input_gid <= 0) die("missing --input, --input-gid or --hide: a run sees its own input alone");
    if (geteuid() != 0) die("must start as root");

    char procs[4096];
    snprintf(procs, sizeof procs, "%s/cgroup.procs", cgroup);
    write_file(procs, "0\n");

    struct rlimit no_core = {0, 0};
    if (setrlimit(RLIMIT_CORE, &no_core) < 0) die("setrlimit RLIMIT_CORE");

    if (unshare(CLONE_NEWNET | CLONE_NEWNS) < 0) die("unshare");
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) < 0) die("make / private");
    loopback_up();
    private_tmpfs("/tmp", tmp_bytes);
    private_tmpfs("/dev/shm", tmp_bytes);
    if (overlay) throwaway_overlay(overlay);
    isolate_input(input, hide);

    int ctl = open(ctl_path, O_WRONLY | O_CLOEXEC);
    if (ctl < 0) die("open %s", ctl_path);
    int ack = open(ack_path, O_RDONLY | O_CLOEXEC);
    if (ack < 0) die("open %s", ack_path);
    int status_fd = open(status_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (status_fd < 0) die("open %s", status_path);
    char **envp = read_env(env_file);

    gid_t groups[1] = {(gid_t)input_gid};
    if (setgroups(1, groups) < 0) die("setgroups");
    if (setresgid((gid_t)gid, (gid_t)gid, (gid_t)gid) < 0) die("setresgid");
    if (setresuid((uid_t)uid, (uid_t)uid, (uid_t)uid) < 0) die("setresuid");
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) die("no_new_privs");
    if (setresuid(0, 0, 0) == 0) { errno = 0; die("privileges were not dropped"); }
    if (chdir(workdir) < 0) die("chdir %s", workdir);

    perf_control(ctl, ack, "enable\n");
    int64_t t0 = now_ns();
    pid_t child = fork();
    if (child < 0) die("fork");
    if (child == 0) {
        execve(argv[command], &argv[command], envp);
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) die("waitpid");
    }
    int64_t t1 = now_ns();
    perf_control(ctl, ack, "disable\n");

    struct rusage usage;
    getrusage(RUSAGE_CHILDREN, &usage);
    char out[512];
    int len;
    if (WIFSIGNALED(status)) {
        len = snprintf(out, sizeof out,
                       "{\"wall_ns\": %lld, \"exit_code\": null, \"signal\": %d, \"maxrss_kb\": %ld, \"t0_ns\": %lld, \"t1_ns\": %lld}\n",
                       (long long)(t1 - t0), WTERMSIG(status), usage.ru_maxrss, (long long)t0, (long long)t1);
    } else {
        len = snprintf(out, sizeof out,
                       "{\"wall_ns\": %lld, \"exit_code\": %d, \"signal\": null, \"maxrss_kb\": %ld, \"t0_ns\": %lld, \"t1_ns\": %lld}\n",
                       (long long)(t1 - t0), WEXITSTATUS(status), usage.ru_maxrss, (long long)t0, (long long)t1);
    }
    if (write(status_fd, out, (size_t)len) != len) die("write status");
    close(status_fd);
    return 0;
}
