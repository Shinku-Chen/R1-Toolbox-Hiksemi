/* R1 Toolbox's own, narrowly scoped Linux ioctl interposer. No vendor code. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Ten percent requested output; never interpreted as a measured RPM or stop. */
#define MIN_TARGET_RAW 26UL

static unsigned int owned_mask;
static char lease_path[1024];
static int (*next_ioctl)(int, unsigned long, ...);

static uint32_t le32(const unsigned char *p) {
    return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24;
}
static uint64_t le64(const unsigned char *p) { return le32(p) | (uint64_t)le32(p+4)<<32; }

__attribute__((constructor)) static void initialize_guard(void) {
    const char *mask = getenv("R1_FAN_MASK");
    const char *path = getenv("R1_FAN_LEASE");
    if (mask && mask[0] >= '1' && mask[0] <= '3' && mask[1] == '\0'
            && path && path[0] == '/' && strlen(path) < sizeof(lease_path)) {
        owned_mask = (unsigned int)(mask[0]-'0');
        memcpy(lease_path, path, strlen(path)+1);
    }
    /* temp_control also launches unrelated helpers. Injection belongs only to
       this one process, never disk-autosleep/exec descendants. */
    unsetenv("LD_PRELOAD");
    unsetenv("R1_FAN_MASK");
    unsetenv("R1_FAN_LEASE");
    next_ioctl = (int (*)(int, unsigned long, ...))dlsym(RTLD_NEXT, "ioctl");
}

static int exact_device(int fd) {
    struct stat actual, expected;
    char name[64], target[1024];
    const char *device = "/dev/hikio";
#ifdef R1_FAN_GUARD_FIXTURE
    /* This branch is compiled only by the stub test, never the shipped .so. */
    device = getenv("R1_FIXTURE_DEVICE");
    if (!device || device[0] != '/') return 0;
#endif
    if (snprintf(name, sizeof(name), "/proc/self/fd/%d", fd) >= (int)sizeof(name)) return 0;
    ssize_t size = readlink(name, target, sizeof(target)-1);
    if (size < 0 || size >= (ssize_t)sizeof(target)-1) return 0;
    target[size] = 0;
    if (strcmp(target, device) || fstat(fd, &actual) || lstat(device, &expected)) return 0;
#ifdef R1_FAN_GUARD_FIXTURE
    if (!S_ISREG(actual.st_mode) || !S_ISREG(expected.st_mode)) return 0;
#else
    if (!S_ISCHR(actual.st_mode) || !S_ISCHR(expected.st_mode)) return 0;
#endif
    return actual.st_dev == expected.st_dev && actual.st_ino == expected.st_ino
        && actual.st_rdev == expected.st_rdev;
}

static unsigned long target_value(unsigned int bit) {
    unsigned char bytes[33];
    struct stat info;
    struct timespec now;
    unsigned long result = 255; /* Lost, corrupt or expired lease: fail high. */
    int fd = open(lease_path, O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
    if (fd < 0) return result;
    if (fstat(fd, &info) == 0 && S_ISREG(info.st_mode) && info.st_uid == geteuid()
            && (info.st_mode & 0777) == 0600 && info.st_size == 32
            && read(fd, bytes, sizeof(bytes)) == 32 && !memcmp(bytes, "R1FAN01\0", 8)
            && clock_gettime(CLOCK_MONOTONIC, &now) == 0) {
        uint64_t tick = (uint64_t)now.tv_sec*1000000000ULL + now.tv_nsec;
        uint64_t until = le64(bytes+8);
        uint32_t mask = le32(bytes+16), value = le32(bytes+(bit == 1 ? 20 : 24));
        if (mask == owned_mask && until > tick && until-tick <= 5000000000ULL
                && value >= MIN_TARGET_RAW && value <= 255 && le32(bytes+28) != 0)
            result = value;
    }
    close(fd);
    return result;
}

int ioctl(int fd, unsigned long request, ...) {
    va_list args;
    va_start(args, request);
    unsigned long argument = va_arg(args, unsigned long);
    va_end(args);
    int saved_errno = errno;
    unsigned int bit = request == 0x5798UL ? 1 : request == 0x5799UL ? 2 : 0;
    if ((owned_mask & bit) && exact_device(fd)) argument = target_value(bit);
    errno = saved_errno;
    if (!next_ioctl) { errno = ENOSYS; return -1; }
    return next_ioctl(fd, request, argument);
}
