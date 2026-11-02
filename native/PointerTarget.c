/* Read-only hit testing. Never send events to a window covering Hearthstone. */
#include <ApplicationServices/ApplicationServices.h>
#include <libproc.h>
#include <math.h>
#include <string.h>

int hs_point_owned(pid_t game, double x, double y) {
    char path[PROC_PIDPATHINFO_MAXSIZE];
    if (game <= 1 || !isfinite(x) || !isfinite(y) || proc_pidpath(game,path,sizeof(path)) <= 0 ||
        strcmp(path,"/Applications/Hearthstone/Hearthstone.app/Contents/MacOS/Hearthstone")) return 0;
    AXUIElementRef system = AXUIElementCreateSystemWide(), hit = NULL;
    if (system) {
        AXUIElementSetMessagingTimeout(system, .2f);
        AXError result = AXUIElementCopyElementAtPosition(system,(float)x,(float)y,&hit);
        CFRelease(system);
        if (hit) {
            pid_t owner = 0;
            AXError identified = AXUIElementGetPid(hit,&owner);
            CFRelease(hit);
            if (result == kAXErrorSuccess && identified == kAXErrorSuccess) return owner == game;
        }
    }
    /* If Accessibility cannot identify an element, require the game itself
       to be the top visible window at this point. Any ambiguity blocks input. */
    CFArrayRef windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,0);
    if (!windows) return 0;
    int allowed = 0;
    for (CFIndex i = 0; i < CFArrayGetCount(windows); ++i) {
        CFDictionaryRef row = CFArrayGetValueAtIndex(windows,i);
        CGRect bounds;
        CFDictionaryRef coordinates = CFDictionaryGetValue(row,kCGWindowBounds);
        if (!coordinates || !CGRectMakeWithDictionaryRepresentation(coordinates,&bounds)) break;
        if (!CGRectContainsPoint(bounds,CGPointMake(x,y))) continue;
        double alpha = 1;
        CFNumberRef opacity = CFDictionaryGetValue(row,kCGWindowAlpha);
        if (opacity && CFNumberGetValue(opacity,kCFNumberDoubleType,&alpha) && alpha <= 0) continue;
        int owner = 0;
        CFNumberRef pid = CFDictionaryGetValue(row,kCGWindowOwnerPID);
        allowed = pid && CFNumberGetValue(pid,kCFNumberIntType,&owner) && owner == game;
        break;
    }
    CFRelease(windows);
    return allowed;
}
