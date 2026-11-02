/* An AC-only wake assertion owned by the GUI's lifetime. No input events. */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/ps/IOPowerSources.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <notify.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/event.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

static IOPMAssertionID display = kIOPMNullAssertionID;

static bool update_power(void) {
    CFTypeRef info = IOPSCopyPowerSourcesInfo();
    CFStringRef source = info ? IOPSGetProvidingPowerSourceType(info) : NULL;
    bool plugged_in = source && CFEqual(source, CFSTR(kIOPMACPowerKey));
    if (info) CFRelease(info);
    if (!plugged_in && display != kIOPMNullAssertionID) {
        IOPMAssertionRelease(display);
        display = kIOPMNullAssertionID;
    }
    if (plugged_in && display == kIOPMNullAssertionID) {
        return IOPMAssertionCreateWithName(kIOPMAssertPreventUserIdleDisplaySleep,
            kIOPMAssertionLevelOn, CFSTR("Hearthstone AI: automatic ranked play and review"),
            &display) == kIOReturnSuccess;
    }
    return true;
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    char *end = NULL;
    long owner = strtol(argv[1], &end, 10);
    if (*end || owner <= 1 || owner > INT_MAX) return 2;
    const char *path = argv[2];
    int lease = open(path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat held;
    if (lease < 0 || fstat(lease, &held) || !S_ISREG(held.st_mode) ||
        held.st_uid != getuid() || held.st_nlink != 1) return 2;
    if (flock(lease, LOCK_EX | LOCK_NB)) {
        if (errno != EWOULDBLOCK) return 2;
        /* Only an initialized holder publishes a PID. */
        char record[32] = {0};
        if (pread(lease, record, sizeof(record) - 1, 0) <= 0) return 2;
        long holder = strtol(record, &end, 10);
        if (*end != '\n' || holder <= 1 || holder > INT_MAX) return 2;
        printf("{\"status\":\"reused\",\"pid\":%ld}\n", holder);
        return 0;
    }
    if (ftruncate(lease, 0)) return 2;
    int power_fd = -1, token = 0, queue = kqueue(), result = 2;
    if (queue < 0 || notify_register_file_descriptor(kIOPSNotifyPowerSource, &power_fd, 0, &token) != NOTIFY_STATUS_OK)
        goto finished;
    struct kevent watch[3], event;
    EV_SET(&watch[0], (uintptr_t)owner, EVFILT_PROC, EV_ADD, NOTE_EXIT, 0, NULL);
    EV_SET(&watch[1], (uintptr_t)lease, EVFILT_VNODE, EV_ADD | EV_CLEAR, NOTE_DELETE | NOTE_RENAME | NOTE_REVOKE, 0, NULL);
    EV_SET(&watch[2], (uintptr_t)power_fd, EVFILT_READ, EV_ADD, 0, 0, NULL);
    if (kevent(queue, watch, 3, NULL, 0, NULL) < 0 || !update_power()) goto finished;
    if (dprintf(lease, "%d\n", getpid()) < 0) goto finished;
    printf("{\"status\":\"started\",\"pid\":%d,\"on_ac\":%s}\n", getpid(),
           display != kIOPMNullAssertionID ? "true" : "false");
    fflush(stdout);
    close(STDOUT_FILENO);
    for (;;) {
        int n = kevent(queue, NULL, 0, &event, 1, NULL);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 || (event.flags & EV_ERROR)) break;
        if (event.filter == EVFILT_PROC || event.filter == EVFILT_VNODE) {
            result = 0;
            break;
        }
        uint32_t notification;
        if (read(power_fd, &notification, sizeof(notification)) != sizeof(notification) || !update_power()) break;
    }
finished:
    if (display != kIOPMNullAssertionID) IOPMAssertionRelease(display);
    if (power_fd >= 0) notify_cancel(token);
    struct stat current;
    if (!lstat(path, &current) && held.st_dev == current.st_dev && held.st_ino == current.st_ino) unlink(path);
    close(lease);
    if (queue >= 0) close(queue);
    return result;
}
