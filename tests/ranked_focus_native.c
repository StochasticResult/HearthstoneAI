/* Exercise the real Carbon hotkey handler and lease without sending user input. */
#include <assert.h>
#include <signal.h>
#include <unistd.h>
#include <Carbon/Carbon.h>
#include <ApplicationServices/ApplicationServices.h>
#include <libproc.h>
#include <string.h>
static int stops;
static int capture_stop(pid_t pid, int sig) { assert(pid == getppid() && sig == SIGTERM); ++stops; return 0; }
static bool locked, front_is_game;
static int activations;
static int fixture_path(int pid, void *buf, uint32_t size) {
    const char *path = pid == 4242 ? "/Applications/Hearthstone/Hearthstone.app/Contents/MacOS/Hearthstone" : "/foreign";
    assert(strlen(path) < size);
    strcpy(buf, path);
    return (int)strlen(path);
}
static CFDictionaryRef fixture_session(void) {
    const void *keys[] = {kCGSessionOnConsoleKey, CFSTR("CGSSessionScreenIsLocked")};
    const void *values[] = {kCFBooleanTrue, locked ? kCFBooleanTrue : kCFBooleanFalse};
    return CFDictionaryCreate(NULL,keys,values,2,&kCFTypeDictionaryKeyCallBacks,&kCFTypeDictionaryValueCallBacks);
}
static int fixture_process(pid_t pid, ProcessSerialNumber *out) {
    assert(pid == 4242); *out = (ProcessSerialNumber){0,42}; return noErr;
}
static int fixture_front(ProcessSerialNumber *out) {
    *out = (ProcessSerialNumber){0,front_is_game ? 42U : 1U}; return noErr;
}
static int fixture_pid(const ProcessSerialNumber *p, pid_t *pid) {
    *pid = p->lowLongOfPSN == 42 ? 4242 : 1234; return noErr;
}
static int fixture_activate(const ProcessSerialNumber *p, unsigned flags) {
    assert(p->lowLongOfPSN == 42 && flags == kSetFrontProcessFrontWindowOnly);
    ++activations; return noErr;
}
#define proc_pidpath fixture_path
#define CGSessionCopyCurrentDictionary fixture_session
#define GetProcessForPID fixture_process
#define GetFrontProcess fixture_front
#define GetProcessPID fixture_pid
#define SetFrontProcessWithOptions fixture_activate
#define kill capture_stop
#define main focus_program
#include "../native/RankedFocus.c"
#undef main

static void deliver(UInt32 kind, UInt32 key_id, double time) {
    EventRef event;
    EventHotKeyID id = {'HSAI',key_id};
    assert(CreateEvent(NULL, kEventClassKeyboard, kind, time, kEventAttributeNone, &event) == noErr);
    assert(SetEventParameter(event, kEventParamDirectObject, typeEventHotKeyID, sizeof(id), &id) == noErr);
    assert(PostEventToQueue(GetMainEventQueue(),event,kEventPriorityStandard) == noErr);
    ReleaseEvent(event);
    EventTypeSpec type = {kEventClassKeyboard,kind};
    assert(ReceiveNextEvent(1,&type,.5,true,&event) == noErr);
    SendEventToEventTarget(event,GetEventDispatcherTarget());
    ReleaseEvent(event);
}

int main(void) {
    FILE *f = tmpfile();
    assert(f);
    EventTypeSpec events[] = {{kEventClassKeyboard,kEventHotKeyPressed},
                              {kEventClassKeyboard,kEventHotKeyReleased}};
    assert(InstallEventHandler(GetEventDispatcherTarget(),hotkey,2,events,NULL,NULL) == noErr);
    owner = getppid(); lease = fileno(f);
    int32_t pid = 1234;
    assert(pwrite(lease,&pid,4,0) == 4);
    tick(); /* This is a test executable, never the claimed game. */
    assert(state == 'X' && attempts == 0);
    pid = 4242; assert(pwrite(lease,&pid,4,0) == 4);
    locked = true;
    tick(); assert(state == 'L' && attempts == 0);
    locked = false;
    tick(); assert(state == 'W' && attempts == 1 && activations == 1);
    tick(); assert(state == 'W' && activations == 1); /* Activation is asynchronous. */
    front_is_game = true;
    tick(); assert(state == 'F' && activations == 1);
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
