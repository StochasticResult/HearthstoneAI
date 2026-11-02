#import <AppKit/AppKit.h>
#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>
#include <unistd.h>

// Development probe only. Event tests never activate Hearthstone, move the
// hardware cursor, or post globally. Production input remains unchanged.
static void fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(2);
}
static void output(NSDictionary *object) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:object options:0 error:nil];
    fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout);
}
static NSDictionary *observation(void) {
    CGEventRef current = CGEventCreate(NULL);
    CGPoint p = CGEventGetLocation(current); CFRelease(current);
    NSRunningApplication *front = NSWorkspace.sharedWorkspace.frontmostApplication;
    return @{@"cursor": @[@(p.x), @(p.y)], @"front_pid": @(front.processIdentifier),
             @"front_bundle": front.bundleIdentifier ?: @""};
}
int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc == 2 && strcmp(argv[1], "prepare-background") == 0) {
            // Setup only: put our existing control app in front before the
            // independent before/after measurements. No game input here.
            for (NSRunningApplication *app in NSWorkspace.sharedWorkspace.runningApplications) {
                if ([app.executableURL.path isEqualToString:[NSHomeDirectory() stringByAppendingPathComponent:@"Applications/Hearthstone AI.app/Contents/MacOS/HearthstoneAI"]]) {
                    BOOL ok = [app activateWithOptions:NSApplicationActivateIgnoringOtherApps];
                    output(@{@"activated_control_app": @(ok)}); return ok ? 0 : 2;
                }
            }
            fail(@"Existing control application not found");
        }
        if (argc == 2 && strcmp(argv[1], "access") == 0) {
            output(@{@"post_access": @(CGPreflightPostEventAccess()), @"state": observation()});
            return 0;
        }
        if (argc != 5 || (strcmp(argv[1], "move") && strcmp(argv[1], "click")) ||
            (strcmp(argv[2], "quartz") && strcmp(argv[2], "window")))
            fail(@"Usage: background-input-probe access | move/click quartz/window X Y");
        if (!CGPreflightPostEventAccess()) fail(@"Event-posting permission is unavailable; no event sent");
        NSArray<NSRunningApplication *> *apps = [NSRunningApplication runningApplicationsWithBundleIdentifier:@"unity.Blizzard Entertainment.Hearthstone"];
        if (apps.count != 1) fail(@"Expected one Hearthstone process");
        pid_t pid = apps[0].processIdentifier;
        if (NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier == pid)
            fail(@"Probe requires Hearthstone to remain in the background");
        NSArray *windows = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID));
        NSDictionary *target = nil;
        for (NSDictionary *row in windows) {
            if ([row[(id)kCGWindowOwnerPID] intValue] == pid && [row[(id)kCGWindowLayer] intValue] == 0 &&
                [row[(id)kCGWindowBounds][@"Width"] doubleValue] > 200) {
                if (target) fail(@"Ambiguous Hearthstone windows");
                target = row;
            }
        }
        if (!target) fail(@"No visible Hearthstone window");
        CGRect frame; CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)target[(id)kCGWindowBounds], &frame);
        char *endX = NULL, *endY = NULL;
        CGPoint point = CGPointMake(strtod(argv[3], &endX), strtod(argv[4], &endY));
        if (*endX || *endY || !isfinite(point.x) || !isfinite(point.y) || !CGRectContainsPoint(frame, point))
            fail(@"Coordinates outside the Hearthstone window");
        int windowID = [target[(id)kCGWindowNumber] intValue];
        NSDictionary *before = observation();
        CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStatePrivate);
        BOOL click = strcmp(argv[1], "click") == 0;
        CGEventType types[] = {kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp};
        for (int i = 0; i < (click ? 3 : 1); ++i) {
            CGEventRef event = NULL;
            if (strcmp(argv[2], "window") == 0) {
                NSPoint local = NSMakePoint(point.x - frame.origin.x, CGRectGetMaxY(frame) - point.y);
                NSEvent *native = [NSEvent mouseEventWithType:(NSEventType)types[i] location:local modifierFlags:0
                    timestamp:NSProcessInfo.processInfo.systemUptime windowNumber:windowID context:nil
                    eventNumber:1 clickCount:(i ? 1 : 0) pressure:(i == 1 ? 1 : 0)];
                event = CGEventCreateCopy(native.CGEvent);
                CGEventSetSource(event, source);
                CGEventSetLocation(event, point);
            } else {
                event = CGEventCreateMouseEvent(source, types[i], point, kCGMouseButtonLeft);
            }
            if (!event) fail(@"Could not create event");
            CGEventSetFlags(event, 0);
            CGEventSetIntegerValueField(event, kCGEventTargetUnixProcessID, pid);
            CGEventSetIntegerValueField(event, kCGMouseEventWindowUnderMousePointer, windowID);
            CGEventSetIntegerValueField(event, kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, windowID);
            if (i) CGEventSetIntegerValueField(event, kCGMouseEventClickState, 1);
            CGEventPostToPid(pid, event);
            CFRelease(event);
            usleep(100000);
        }
        CFRelease(source);
        output(@{@"transport": [NSString stringWithUTF8String:argv[2]], @"command": [NSString stringWithUTF8String:argv[1]],
                 @"target_pid": @(pid), @"window": @(windowID), @"before": before, @"after": observation()});
    }
    return 0;
}
