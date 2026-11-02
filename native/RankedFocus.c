/* The ranked session owns this helper. It posts no input and can only activate
 * the existing Hearthstone process that the game watchdog claimed. */
#include <Carbon/Carbon.h>
#include <ApplicationServices/ApplicationServices.h>
#include <libproc.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static const char game_path[] = "/Applications/Hearthstone/Hearthstone.app/Contents/MacOS/Hearthstone";
static pid_t owner, first_game;
static ProcessSerialNumber first_serial;
static int lease;
static char state;
static uint32_t attempts;
static double last_activation = -10, first_press = -10;
static bool held, stopped;

static void publish(char value) {
    if (state != value) {
        if (pwrite(lease, &value, 1, 4) != 1) _exit(2);
        state = value;
    }
}

static bool stop_gesture(bool pressed, double now) {
    if (!pressed) { held = false; return false; }
    if (held) return false;
    held = true;
    bool twice = now >= first_press && now - first_press <= 3;
    first_press = now;
    return twice;
}

static OSStatus hotkey(EventHandlerCallRef call, EventRef event, void *context) {
    (void)call; (void)context;
    EventHotKeyID id;
    if (GetEventParameter(event, kEventParamDirectObject, typeEventHotKeyID, NULL,
                          sizeof(id), NULL, &id) != noErr || id.signature != 'HSAI' || id.id != 1)
        return eventNotHandledErr;
    if (!stopped && stop_gesture(GetEventKind(event) == kEventHotKeyPressed, GetEventTime(event))) {
        stopped = true;
        publish('S');  /* Latch the stop even if a SIGTERM arrives during cleanup. */
        if (getppid() == owner) kill(owner, SIGTERM);
    }
    return noErr;
}

static bool console_available(void) {
    CFDictionaryRef session = CGSessionCopyCurrentDictionary();
    if (!session) return false;
    CFTypeRef console = CFDictionaryGetValue(session, kCGSessionOnConsoleKey);
    CFTypeRef locked = CFDictionaryGetValue(session, CFSTR("CGSSessionScreenIsLocked"));
    bool available = console == kCFBooleanTrue && (!locked || locked == kCFBooleanFalse);
    CFRelease(session);
    return available;
}

static void tick(void) {
    if (getppid() != owner) _exit(0);
    if (stopped) return;
    int32_t target;
    if (pread(lease, &target, sizeof(target), 0) != sizeof(target)) _exit(2);
    if (target == 0) { publish('I'); return; }
    if (target <= 1 || (first_game && first_game != target)) { publish('X'); return; }
    char path[PROC_PIDPATHINFO_MAXSIZE];
    ProcessSerialNumber game, front;
    if (proc_pidpath(target, path, sizeof(path)) <= 0 || strcmp(path, game_path)) { publish('X'); return; }
    if (!console_available()) { publish('L'); return; }
    if (GetProcessForPID(target, &game) != noErr) { publish('W'); return; }
    if (first_game && (game.highLongOfPSN != first_serial.highLongOfPSN ||
                       game.lowLongOfPSN != first_serial.lowLongOfPSN)) { publish('X'); return; }
    first_game = target;
    first_serial = game;
    pid_t front_pid = 0;
    if (GetFrontProcess(&front) == noErr && GetProcessPID(&front, &front_pid) == noErr &&
        front_pid == target) { publish('F'); return; }
    publish('W');
    double now = GetCurrentEventTime();
    if (now - last_activation >= 2) {
        last_activation = now;
        ++attempts;
        if (pwrite(lease, &attempts, sizeof(attempts), 5) != sizeof(attempts)) _exit(2);
        SetFrontProcessWithOptions(&game, kSetFrontProcessFrontWindowOnly);
        /* A later foreground query must succeed before a click can proceed. */
    }
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    owner = (pid_t)atoi(argv[1]);
    lease = atoi(argv[2]);
    if (owner <= 1 || getppid() != owner || lease < 3) return 2;
    EventTypeSpec events[] = {{kEventClassKeyboard,kEventHotKeyPressed},
                              {kEventClassKeyboard,kEventHotKeyReleased}};
    EventTargetRef target = GetEventDispatcherTarget();
    EventHotKeyRef key;
    if (InstallEventHandler(target, hotkey, 2, events, NULL, NULL) != noErr ||
        RegisterEventHotKey(kVK_ANSI_K, controlKey | optionKey | cmdKey, (EventHotKeyID){'HSAI',1},
                            target, kEventHotKeyExclusive, &key) != noErr) return 2;
    publish('I');
    puts("{\"status\":\"armed\"}"); fflush(stdout);
    for (;;) {
        EventRef event;
        OSStatus result = ReceiveNextEvent(2, events, .25, true, &event);
        if (result == noErr) {
            SendEventToEventTarget(event, target);
            ReleaseEvent(event);
        } else if (result != eventLoopTimedOutErr) return 2;
        tick();
    }
}
