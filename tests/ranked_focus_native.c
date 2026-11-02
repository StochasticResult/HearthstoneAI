/* Exercise the real Carbon hotkey handler and lease without sending user input. */
#include <assert.h>
#include <signal.h>
#include <unistd.h>
static int stops;
static int capture_stop(pid_t pid, int sig) { assert(pid == getppid() && sig == SIGTERM); ++stops; return 0; }
#define kill capture_stop
#define main focus_program
#include "../native/RankedFocus.c"
#undef main

static void deliver(UInt32 kind, UInt32 key_id, double time) {
    EventRef event;
    EventHotKeyID id = {'HSAI',key_id};
    assert(CreateEvent(NULL, kEventClassKeyboard, kind, time, kEventAttributeNone, &event) == noErr);
    assert(SetEventParameter(event, kEventParamDirectObject, typeEventHotKeyID, sizeof(id), &id) == noErr);
    hotkey(NULL,event,NULL);
    ReleaseEvent(event);
}

int main(void) {
    FILE *f = tmpfile();
    assert(f);
    owner = getppid(); lease = fileno(f);
    int32_t pid = getpid();
    assert(pwrite(lease,&pid,4,0) == 4);
    tick(); /* This is a test executable, never the claimed game. */
    assert(state == 'X' && attempts == 0);
    pid = 0; assert(pwrite(lease,&pid,4,0) == 4);
    tick(); assert(state == 'I');
    deliver(kEventHotKeyPressed,2,10); /* Unrelated key. */
    deliver(kEventHotKeyPressed,1,11);
    deliver(kEventHotKeyPressed,1,11.2); /* Key repeat without release. */
    deliver(kEventHotKeyReleased,1,11.3);
    deliver(kEventHotKeyPressed,1,15); /* First press expired. */
    assert(stops == 0 && state == 'I');
    deliver(kEventHotKeyReleased,1,15.2);
    deliver(kEventHotKeyPressed,1,16);
    tick(); char value = 0; assert(pread(lease,&value,1,4) == 1);
    assert(stopped && stops == 1 && value == 'S');
    fclose(f);
    return 0;
}
